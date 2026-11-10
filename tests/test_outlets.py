"""Power strip logic against a fake strip and a fake clock.
Run: python -m unittest discover -s tests -t ."""
import tempfile
import unittest
from pathlib import Path

from app import store
from app.config import Config, Outlet
from app.outlets import StripService

OUTLETS = (Outlet(1, "return", "Return 1"), Outlet(2, "return", "Return 2"),
           Outlet(3, "circulation", "Circulation 1"), Outlet(4, "circulation", "Circulation 2"),
           Outlet(5, "skimmer", "Skimmer"), Outlet(6, "ato", "ATO"))
DEFAULTS = {"feed_minutes": 10, "skimmer_delay_minutes": 5, "maintenance_white_percent": 5,
            "ato_long_run_seconds": 120, "ato_runs_factor": 2.0}
T0 = 1_800_000_000.0


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


class FakeStrip:
    """Behaves like the P316M as the driver presents it: outlets, countdowns
    that fire on their own, on_time since the last change, power-cut defaults."""

    def __init__(self, clock: Clock):
        self.clock = clock
        self.state = {p: True for p in range(1, 7)}
        self.changed = {p: clock() - 3600 for p in range(1, 7)}
        self.watts = {1: 40.0, 2: 40.0, 3: 18.0, 4: 18.0, 5: 25.0, 6: 1.0}
        self.countdown: dict[int, float] = {}
        self.default_on = {p: p != 5 for p in range(1, 7)}
        self.calls = []

    def _set(self, p, on):
        if self.state[p] != on:
            self.state[p] = on
            self.changed[p] = self.clock()

    def _fire(self):
        for p, at in list(self.countdown.items()):
            if self.clock() >= at:
                self._set(p, True)
                del self.countdown[p]

    def power_cut(self):
        self.countdown.clear()
        for p in self.state:
            self.state[p] = self.default_on[p]
            self.changed[p] = self.clock()

    async def read(self):
        self._fire()
        return {"outlets": {p: {"on": self.state[p], "watts": self.watts[p] if self.state[p] else 0.0,
                                "on_time": int(self.clock() - self.changed[p]) if self.state[p] else 0,
                                "today_kwh": 0.1, "default_on": self.default_on[p]}
                            for p in self.state},
                "clock": None, "firmware": "1.0.5"}

    async def switch(self, changes):
        self.calls.append(("switch", dict(changes)))
        for p, change in changes.items():
            if isinstance(change, tuple):
                self._set(p, False)
                self.countdown[p] = self.clock() + change[1]
            else:
                self.countdown.pop(p, None)
                self._set(p, change)

    async def countdown_on(self, p, seconds):
        self.calls.append(("countdown_on", p, seconds))
        self.countdown[p] = self.clock() + seconds

    async def countdowns(self):
        return {p: {"on": True, "remain": int(at - self.clock())} for p, at in self.countdown.items()}

    async def ensure_settings(self, power_cut_on):
        self.calls.append(("ensure_settings", power_cut_on))
        return []

    async def set_clock(self):
        pass

    async def close(self):
        pass


class FakeLights:
    def __init__(self):
        self.calls = []

    async def hold_white(self, permille):
        self.calls.append(("hold", permille))

    async def release(self):
        self.calls.append(("release",))


class StripTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._db = store.DB_PATH
        store.DB_PATH = Path(self.tmp.name) / "test.db"
        self.clock = Clock()
        self.strip = FakeStrip(self.clock)
        self.lights = FakeLights()
        cfg = Config(port=0, prime_hd_host="", prime_16hd_address="", prime_16hd_serial="",
                     live_idle_timeout=120, strip_host="x", strip_poll_seconds=5,
                     outlets=OUTLETS, defaults=DEFAULTS)
        self.svc = StripService(cfg, self.strip, self.lights, clock=self.clock)
        await self.svc.tick()

    async def asyncTearDown(self):
        store.DB_PATH = self._db
        self.tmp.cleanup()

    async def advance(self, seconds, step=5):
        end = self.clock.t + seconds
        while self.clock.t < end:
            self.clock.t = min(end, self.clock.t + step)
            await self.svc.tick()

    def outlet(self, pos):
        return next(o for o in self.svc.snapshot()["outlets"] if o["position"] == pos)

    # ---- set-up ----------------------------------------------------------------

    async def test_startup_fixes_power_cut_settings(self):
        self.assertIn(("ensure_settings", {1: True, 2: True, 3: True, 4: True, 5: False, 6: True}),
                      self.strip.calls)

    # ---- feed ----------------------------------------------------------------------

    async def test_feed_turns_pumps_and_skimmer_off_with_strip_timers(self):
        await self.svc.feed("start")
        self.assertEqual({p: self.strip.state[p] for p in range(1, 7)},
                         {1: False, 2: False, 3: False, 4: False, 5: False, 6: True})
        self.assertEqual(self.strip.countdown[1], T0 + 600)
        self.assertEqual(self.strip.countdown[5], T0 + 900)   # skimmer 5 min after the pumps
        self.assertEqual(self.svc.snapshot()["mode"]["name"], "feed")

        await self.advance(605)
        self.assertTrue(all(self.strip.state[p] for p in (1, 2, 3, 4)))
        self.assertFalse(self.strip.state[5])
        await self.advance(300)
        self.assertTrue(self.strip.state[5])
        self.assertIsNone(self.svc.snapshot()["mode"])
        self.assertEqual(self.svc.timers, {})
        self.assertEqual(self.svc.snapshot()["warnings"], [])

    async def test_feed_end_now_brings_pumps_back_and_delays_skimmer(self):
        await self.svc.feed("start")
        await self.advance(60)
        await self.svc.feed("end")
        self.assertTrue(all(self.strip.state[p] for p in (1, 2, 3, 4)))
        self.assertFalse(self.strip.state[5])
        self.assertEqual(self.strip.countdown[5], self.clock.t + 300)
        await self.advance(305)
        self.assertTrue(self.strip.state[5])

    async def test_pc_crash_mid_feed_strip_still_restores(self):
        await self.svc.feed("start")
        self.clock.t += 2 * 3600             # dashboard gone; the strip's own timers run
        await self.svc.tick()
        self.assertTrue(all(self.strip.state.values()))
        self.assertIsNone(self.svc.snapshot()["mode"])

    async def test_feed_leaves_a_held_off_skimmer_alone(self):
        await self.svc.outlet(5, "off")
        await self.svc.feed("start")
        self.assertNotIn(5, self.strip.countdown)
        await self.advance(1200)
        self.assertFalse(self.strip.state[5])

    # ---- maintenance ---------------------------------------------------------------

    async def test_maintenance_turns_everything_off_without_timers(self):
        await self.svc.maintenance("start")
        self.assertFalse(any(self.strip.state.values()))
        self.assertEqual(self.strip.countdown, {})
        self.assertEqual(self.lights.calls, [("hold", 50)])
        await self.advance(3 * 3600, step=60)
        self.assertFalse(any(self.strip.state.values()))  # stays off until Done

        await self.svc.maintenance("done")
        self.assertTrue(all(self.strip.state[p] for p in (1, 2, 3, 4)))
        self.assertFalse(self.strip.state[5] or self.strip.state[6])
        self.assertEqual(self.lights.calls[-1], ("release",))
        await self.advance(305)
        self.assertTrue(self.strip.state[5] and self.strip.state[6])
        self.assertIsNone(self.svc.snapshot()["mode"])

    async def test_power_cut_during_maintenance_ends_it(self):
        await self.svc.maintenance("start")
        self.strip.power_cut()
        await self.svc.tick()
        self.assertIsNone(self.svc.mode)
        self.assertEqual(self.lights.calls[-1], ("release",))
        self.assertFalse(self.strip.state[5])
        await self.advance(310)
        self.assertTrue(self.strip.state[5])  # skimmer restarted after the pumps ran 5 min

    # ---- power cut & skimmer ----------------------------------------------------------

    async def test_skimmer_restarts_after_pumps_have_run(self):
        self.strip.power_cut()
        await self.svc.tick()
        self.assertFalse(self.strip.state[5])
        self.assertGreater(self.outlet(5)["restart_in"], 0)
        await self.advance(240)
        self.assertFalse(self.strip.state[5])
        await self.advance(65)
        self.assertTrue(self.strip.state[5])
        self.assertIsNone(self.outlet(5)["restart_in"])

    async def test_held_off_skimmer_stays_off_after_power_cut(self):
        await self.svc.outlet(5, "off")
        self.strip.power_cut()
        await self.advance(900)
        self.assertFalse(self.strip.state[5])
        self.assertTrue(self.outlet(5)["held_off"])

    async def test_pumps_never_go_off_without_a_timer(self):
        with self.assertRaises(ValueError):
            await self.svc.outlet(1, "off")
        await self.svc.outlet(1, "off_for", 30)
        self.assertEqual(self.strip.countdown[1], self.clock.t + 1800)

    async def test_late_strip_timer_is_backed_up_by_the_dashboard(self):
        await self.svc.outlet(3, "off_for", 1)
        self.strip.countdown.clear()          # the strip lost its timer
        await self.advance(60 + 95)
        self.assertTrue(self.strip.state[3])

    async def test_pump_switched_off_at_the_strip_is_flagged(self):
        self.strip._set(2, False)
        await self.svc.tick()
        self.assertTrue(self.outlet(2)["unexpected_off"])
        self.assertTrue(any("Return 2" in w for w in self.svc.snapshot()["warnings"]))

    # ---- ATO & health -----------------------------------------------------------------

    async def test_ato_runs_are_logged(self):
        await self.advance(120)               # learn the idle draw
        self.strip.watts[6] = 9.0
        await self.advance(30)
        self.strip.watts[6] = 1.0
        await self.advance(10)
        ato = self.svc.ato_summary()
        self.assertEqual(ato["count"], 1)
        self.assertAlmostEqual(ato["runs"][0]["seconds"], 30, delta=5)

    async def test_long_ato_run_warns(self):
        await self.advance(120)
        self.strip.watts[6] = 9.0
        await self.advance(150)
        self.assertTrue(any("ATO has been running" in w for w in self.svc.snapshot()["warnings"]))

    async def test_outlet_history_has_minute_averages(self):
        await self.advance(185)
        h = self.svc.outlet_history(1, hours=1)
        self.assertGreaterEqual(len(h["points"]), 3)
        self.assertEqual(h["points"][-1][1], 40.0)
        self.assertTrue(h["points"][-1][2])
        long = self.svc.outlet_history(1, hours=72)
        self.assertEqual(long["step_minutes"], 10)

    async def test_ato_history_counts_runs_per_day(self):
        await self.advance(120)
        self.strip.watts[6] = 9.0
        await self.advance(30)
        self.strip.watts[6] = 1.0
        await self.advance(10)
        hist = self.svc.ato_history(days=7)
        self.assertEqual(len(hist["days"]), 7)
        self.assertEqual(hist["days"][-1]["runs"], 1)
        self.assertEqual(len(hist["runs"]), 1)

    async def test_empty_outlet_never_learns_zero_as_normal(self):
        minute = int(self.clock.t // 60)
        store.add_outlet_minutes([(minute - 100 + i, 1, 0.0, True) for i in range(90)])  # empty for 90 min
        self.strip.watts[1] = 40.0                       # then a pump gets plugged in
        await self.advance(300)
        h = self.outlet(1)["health"]                     # must not crash (it divided by 0 before)
        self.assertEqual(h["state"], "learning")
        self.assertIsNone(h["normal"])

    async def test_pump_drawing_far_less_than_normal_warns(self):
        minute = int(self.clock.t // 60)
        store.add_outlet_minutes([(minute - 100 + i, 1, 40.0, True) for i in range(90)])
        self.strip.watts[1] = 15.0
        await self.advance(300)
        h = self.outlet(1)["health"]
        self.assertEqual(h["state"], "warn")
        self.assertIn("below normal", h["text"])
        self.assertEqual(self.outlet(2)["health"]["state"], "learning")


if __name__ == "__main__":
    unittest.main()
