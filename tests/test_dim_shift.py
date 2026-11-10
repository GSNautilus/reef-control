"""Schedule shift and dim mode: the math, and writing them to (fake) lights
that start with the schedules backed up on 2026-11-04. Run with the other tests."""
import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path

from app import schedule as s
from app import services, store

BACKUPS = Path(__file__).resolve().parent.parent / "backups"


def load_16hd_backup() -> list[dict]:
    data = json.loads((BACKUPS / "16hd_schedule_before.json").read_text())
    return [{"time": p["time_minutes"], "flags": p["flags"], "channels": p["channels"]} for p in data["schedule1"]]


def hd_backup() -> dict:
    return json.loads((BACKUPS / "primehd_schedule_before.json").read_text())


class FakeScheduleLight:
    """Holds a schedule in the light's own format, like the real drivers."""

    def __init__(self, schedule, fail_write=False, fail_after_write=False):
        self.schedule = copy.deepcopy(schedule)
        self.writes = 0
        self.fail_write = fail_write
        self.fail_after_write = fail_after_write

    async def get_schedule(self):
        return copy.deepcopy(self.schedule)

    async def set_schedule(self, payload):
        if self.fail_write:
            raise RuntimeError("light busy")
        self.writes += 1
        self.schedule = copy.deepcopy(payload)
        if self.fail_after_write:
            raise RuntimeError("connection dropped after the write")


def make_lights(hd, hd16) -> services.Lights:
    lights = object.__new__(services.Lights)
    lights.hd, lights.hd16 = hd, hd16
    lights._writing = asyncio.Lock()
    return lights


class ShiftMath(unittest.TestCase):
    def test_shift_wraps_midnight(self):
        pts = s.from_prime_hd(hd_backup())
        later = s.shifted(pts, 120)
        self.assertEqual([p.time for p in later], [60, 720, 840, 960, 1200, 1320, 1410])
        self.assertEqual([p.time for p in s.shifted(later, -120)], [p.time for p in pts])

    def test_effective_shift_keeps_the_lights_channels(self):
        out = s.effective([s.Point(600, {"blue": 0}), s.Point(720, {"blue": 500})], s.Tweak(), "prime_hd", -120)
        self.assertEqual([p.time for p in out], [480, 600])
        self.assertNotIn("moonlight", out[0].levels)

    def test_shift_past_midnight_round_trips_on_the_prime_hd(self):
        pts = s.from_prime_hd(hd_backup())
        later = s.shifted(pts, 120)
        back = s.from_prime_hd(s.to_prime_hd(later))
        self.assertEqual([(p.time, p.levels) for p in back], [(p.time, p.levels) for p in later])

    def test_16hd_flags_follow_the_shift(self):
        pts = s.shifted(s.from_prime_16hd(load_16hd_backup()), -120)
        self.assertEqual([p["flags"] for p in s.to_prime_16hd(pts)], [3, 7, 1, 1, 1, 1, 11])

    def test_clamp_shift(self):
        self.assertEqual(s.clamp_shift(-900), -720)
        self.assertEqual(s.clamp_shift("90"), 90)
        self.assertEqual(s.clamp_shift(None), 0)


class DimMath(unittest.TestCase):
    def test_scales_and_keeps_lit_channels_lit(self):
        out = s.dimmed([s.Point(600, {"blue": 500, "uv": 3, "green": 0})], 10)
        self.assertEqual(out[0].levels, {"blue": 50, "uv": 1, "green": 0})

    def test_none_is_unchanged(self):
        pts = [s.Point(600, {"blue": 500})]
        self.assertEqual(s.dimmed(pts, None)[0].levels, {"blue": 500})

    def test_dimmed_16hd_keeps_its_flags(self):
        pts = s.dimmed(s.from_prime_16hd(load_16hd_backup()), 10)
        self.assertEqual([p["flags"] for p in s.to_prime_16hd(pts)], [3, 7, 1, 1, 1, 1, 11])


class DimMode(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._db, self._backups = store.DB_PATH, services.BACKUP_DIR
        store.DB_PATH = Path(self.tmp.name) / "test.db"
        services.BACKUP_DIR = Path(self.tmp.name) / "backups"
        self.hd = FakeScheduleLight(hd_backup())
        self.hd16 = FakeScheduleLight(load_16hd_backup())
        self.lights = make_lights(self.hd, self.hd16)

    async def asyncTearDown(self):
        store.DB_PATH, services.BACKUP_DIR = self._db, self._backups
        self.tmp.cleanup()

    def assertOriginal(self):
        self.assertTrue(s.prime_hd_equal(self.hd.schedule, hd_backup()))
        self.assertTrue(s.prime_16hd_equal(self.hd16.schedule, load_16hd_backup()))

    def hd_blue_at(self, minute):
        return dict(s.levels_at(s.from_prime_hd(self.hd.schedule), minute))["blue"]

    async def test_dim_on_scales_and_off_restores_exactly(self):
        full = self.hd_blue_at(840)
        out = await self.lights.set_dim(True, 70)
        self.assertTrue(all(r["ok"] for r in out["results"].values()))
        self.assertEqual(self.hd_blue_at(840), round(full * 0.7))
        self.assertEqual(out["dim"]["lights"], {"prime_hd": 70, "prime_16hd": 70})
        self.assertEqual(out["dim"]["behind"], [])
        await self.lights.set_dim(False, 70)
        self.assertOriginal()

    async def test_changing_the_level_never_drifts(self):
        for pct in (70, 55, 85, 33, 90):
            await self.lights.set_dim(True, pct)
        await self.lights.set_dim(False, 90)
        self.assertOriginal()

    async def test_level_change_while_off_only_saves_it(self):
        out = await self.lights.set_dim(False, 40)
        self.assertEqual(out["results"], {})
        self.assertEqual((self.hd.writes, self.hd16.writes), (0, 0))
        self.assertEqual(services.dim_state(), {"on": False, "percent": 40})

    async def test_rejects_out_of_range(self):
        with self.assertRaises(ValueError):
            await self.lights.set_dim(True, 0)

    async def test_only_one_write_at_a_time(self):
        async with self.lights._writing:
            with self.assertRaises(services.Busy):
                await self.lights.set_dim(True, 70)

    async def test_failed_light_is_behind_until_retried(self):
        self.hd16.fail_write = True
        out = await self.lights.set_dim(True, 60)
        self.assertFalse(out["results"]["prime_16hd"]["ok"])
        self.assertEqual(out["dim"]["behind"], ["prime_16hd"])
        self.hd16.fail_write = False
        out = await self.lights.set_dim(True, 60)
        self.assertEqual(out["dim"]["behind"], [])

    async def test_write_that_failed_half_way_isnt_dimmed_twice(self):
        self.hd16.fail_after_write = True  # the light got the dimmed schedule, but no read-back
        await self.lights.set_dim(True, 50)
        self.hd16.fail_after_write = False
        await self.lights.set_dim(False, 50)
        self.assertOriginal()

    async def test_send_keeps_dim_and_shift_and_import_takes_them_out(self):
        base = s.from_prime_hd(hd_backup())
        draft = {**services.default_draft(), "points": [p.to_json() for p in base], "shift": -120}
        await self.lights.set_dim(True, 80)
        out = await self.lights.send(draft)
        self.assertTrue(all(r["ok"] for r in out["results"].values()))
        on_light = s.from_prime_hd(self.hd.schedule)
        self.assertEqual(on_light[0].time, 480)  # 10:00 two hours earlier
        self.assertEqual(on_light[2].levels["blue"], round(base[2].levels["blue"] * 0.8))
        entry = store.list_history()[0]
        self.assertEqual((entry["draft"]["shift"], entry["draft"]["dim"]), (-120, 80))

        for light in s.LIGHTS:
            imported = await self.lights.import_schedule(light)
            self.assertEqual([p.time for p in imported], [p.time for p in base])
            self.assertEqual([p.levels["blue"] for p in imported], [p.levels["blue"] for p in base])

        await self.lights.set_dim(False, 80)  # still shifted, back to full
        on_light = s.from_prime_hd(self.hd.schedule)
        self.assertEqual(on_light[0].time, 480)
        self.assertEqual(on_light[2].levels["blue"], base[2].levels["blue"])

    async def test_draft_view_shows_what_each_light_runs(self):
        base = s.from_prime_hd(hd_backup())
        draft = {**services.default_draft(), "points": [p.to_json() for p in base], "shift": -60}
        view = services.draft_view(draft, 50)
        self.assertEqual(view["shift"], -60)
        self.assertEqual(view["points"][0]["time"], 600)  # the base keeps its times
        hd = view["effective"]["prime_hd"]
        self.assertEqual(hd[0]["time"], 540)
        self.assertEqual(hd[2]["levels"]["blue"], max(1, round(base[2].levels["blue"] * 0.5)))


if __name__ == "__main__":
    unittest.main()
