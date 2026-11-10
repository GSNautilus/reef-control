"""Round-trip tests against the schedules backed up from both lights on
2026-11-04. Run: python -m unittest discover -s tests"""
import json
import unittest
from pathlib import Path

from app import schedule as s

BACKUPS = Path(__file__).resolve().parent.parent / "backups"


def load_16hd_backup() -> list[dict]:
    data = json.loads((BACKUPS / "16hd_schedule_before.json").read_text())
    return [{"time": p["time_minutes"], "flags": p["flags"], "channels": p["channels"]} for p in data["schedule1"]]


class PrimeHdRoundTrip(unittest.TestCase):
    def test_backup_round_trips(self):
        original = json.loads((BACKUPS / "primehd_schedule_before.json").read_text())
        points = s.from_prime_hd(original)
        self.assertEqual([p.time for p in points], [600, 720, 840, 1080, 1200, 1290, 1380])
        self.assertTrue(s.prime_hd_equal(s.to_prime_hd(points), original))

    def test_midnight_point_is_added(self):
        pts = [s.Point(600, {"blue": 0}), s.Point(720, {"blue": 500}), s.Point(1380, {"blue": 0})]
        ramps = {r["color"]: r["points"] for r in s.to_prime_hd(pts)["ramps"]}
        self.assertEqual(ramps["blue"][0], {"time": 0, "intensity": 0})

    def test_lit_midnight_is_interpolated(self):
        pts = [s.Point(1380, {"blue": 400}), s.Point(60, {"blue": 200}), s.Point(720, {"blue": 0})]
        ramps = {r["color"]: r["points"] for r in s.to_prime_hd(pts)["ramps"]}
        self.assertEqual(ramps["blue"][0], {"time": 0, "intensity": 300})  # 60 of 120 min from 400 to 200
        self.assertEqual([p.time for p in s.from_prime_hd(s.to_prime_hd(pts))], [60, 720, 1380])


class Prime16hdRoundTrip(unittest.TestCase):
    def test_backup_round_trips_with_flags(self):
        original = load_16hd_backup()
        points = s.from_prime_16hd(original)
        self.assertEqual([p.time for p in points], [600, 720, 840, 1080, 1200, 1290, 1380])
        rebuilt = s.to_prime_16hd(points)
        self.assertEqual([p["flags"] for p in rebuilt], [3, 7, 1, 1, 1, 1, 11])
        self.assertTrue(s.prime_16hd_equal(rebuilt, original))

    def test_both_lights_share_nearly_the_same_schedule(self):
        hd = s.from_prime_hd(json.loads((BACKUPS / "primehd_schedule_before.json").read_text()))
        hd16 = s.from_prime_16hd(load_16hd_backup())
        diffs = [(a.time, c) for a, b in zip(hd, hd16) for c in s.SHARED_CHANNELS if a.levels[c] != b.levels[c]]
        self.assertEqual(diffs, [(1200, "cool_white")])


class Tweaks(unittest.TestCase):
    def test_scale_and_offset_leave_dark_points_dark(self):
        pts = [s.Point(600, {"blue": 0}), s.Point(840, {"blue": 400, "uv": 100})]
        out = s.effective(pts, s.Tweak(90, {"blue": 50, "uv": -200}), "prime_16hd")
        self.assertEqual(out[0].levels["blue"], 0)
        self.assertEqual(out[1].levels["blue"], 410)
        self.assertEqual(out[1].levels["uv"], 0)

    def test_clamped_to_full(self):
        out = s.effective([s.Point(0, {"blue": 900}), s.Point(60, {})], s.Tweak(150), "prime_hd")
        self.assertEqual(out[0].levels["blue"], 1000)
        self.assertNotIn("moonlight", out[0].levels)


if __name__ == "__main__":
    unittest.main()
