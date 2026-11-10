"""Live control holds until "Back to schedule", survives restarts, and gives a
held setting back after maintenance. Fake lights; run with the other tests."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app import services, store
from app.services import HOLD_RENEW, HOLD_SECONDS, LiveControl, MaintenanceLights


class FakeHD:
    def __init__(self):
        self.calls = []

    async def set_manual(self, levels):
        self.calls.append(("manual", levels.get("blue", 0)))

    async def set_levels(self, levels):
        self.calls.append(("levels", levels.get("blue", 0)))

    async def resume_schedule(self):
        self.calls.append(("schedule",))

    async def set_off(self):
        self.calls.append(("off",))


class FakeHD16:
    def __init__(self):
        self.calls = []

    async def set_live(self, levels, seconds):
        self.calls.append(("live", levels.get("blue", 0), seconds))

    async def stop_live(self, then_off=False):
        self.calls.append(("stop", then_off))


class FakePower:
    def __init__(self):
        self.off = set()

    def is_off(self, light):
        return light in self.off


class LiveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._db = store.DB_PATH
        store.DB_PATH = Path(self.tmp.name) / "test.db"
        self.hd, self.hd16, self.power = FakeHD(), FakeHD16(), FakePower()
        self.live = LiveControl(SimpleNamespace(hd=self.hd, hd16=self.hd16), 120, self.power)

    async def asyncTearDown(self):
        for task in (self.live._ticker, self.live._16hd_task):
            if task:
                task.cancel()
        store.DB_PATH = self._db
        self.tmp.cleanup()

    async def settle(self):
        if self.live._16hd_task:
            await self.live._16hd_task

    async def test_holds_with_no_idle_timeout(self):
        await self.live.set(["prime_hd", "prime_16hd"], {"blue": 300})
        await self.settle()
        self.live.last_change -= 3600            # an hour without touching anything
        await asyncio.sleep(0)
        self.assertTrue(self.live.active)
        self.assertEqual(self.hd.calls, [("manual", 300)])            # manual mode: holds with the PC off
        self.assertEqual(self.hd16.calls, [("live", 300, HOLD_SECONDS)])

    async def test_later_changes_dont_reenter_manual_mode(self):
        await self.live.set(["prime_hd"], {"blue": 300})
        await self.live.set(["prime_hd"], {"blue": 400})
        self.assertEqual(self.hd.calls, [("manual", 300), ("levels", 400)])

    async def test_back_to_schedule_ends_the_hold(self):
        await self.live.set(["prime_hd", "prime_16hd"], {"blue": 300})
        await self.settle()
        await self.live.stop()
        self.assertFalse(self.live.active)
        self.assertEqual(self.hd.calls[-1], ("schedule",))
        self.assertEqual(self.hd16.calls[-1], ("stop", False))
        self.assertEqual((store.get("live") or {}).get("levels"), {})

    async def test_hold_is_restored_after_a_restart(self):
        await self.live.set(["prime_hd", "prime_16hd"], {"blue": 300})
        await self.settle()
        hd, hd16 = FakeHD(), FakeHD16()
        restarted = LiveControl(SimpleNamespace(hd=hd, hd16=hd16), 120, self.power)
        await restarted.restore()
        if restarted._16hd_task:
            await restarted._16hd_task
        self.assertEqual(set(restarted.levels), {"prime_hd", "prime_16hd"})
        self.assertEqual(hd.calls, [("manual", 300)])
        self.assertEqual(hd16.calls, [("live", 300, HOLD_SECONDS)])
        restarted._ticker.cancel()

    async def test_shutdown_leaves_a_held_setting_on_the_lights(self):
        await self.live.set(["prime_hd", "prime_16hd"], {"blue": 300})
        await self.live.close()                   # Quit in the notification area, or Ctrl+C
        await asyncio.sleep(0)
        self.assertEqual(self.hd.calls, [("manual", 300)])                 # not handed back to schedule
        self.assertEqual(self.hd16.calls, [("live", 300, HOLD_SECONDS)])   # newest levels sent, no stop
        self.assertTrue(self.live._ticker.cancelled())
        self.assertEqual(set(store.get("live")["levels"]), {"prime_hd", "prime_16hd"})  # re-applied next start

    async def test_16hd_scene_is_renewed(self):
        await self.live.set(["prime_16hd"], {"blue": 300})
        await self.settle()
        self.live._16hd_sent -= HOLD_RENEW + 1    # pretend 4 h have passed
        self.live._ticker.cancel()
        orig = services.LIVE_TICK
        services.LIVE_TICK = 0.001
        try:
            self.live._ticker = asyncio.create_task(self.live._tick())
            await asyncio.sleep(0.1)
            await self.settle()
        finally:
            services.LIVE_TICK = orig
        self.assertEqual(len([c for c in self.hd16.calls if c[0] == "live"]), 2)

    async def test_maintenance_gives_a_held_setting_back(self):
        await self.live.set(["prime_hd", "prime_16hd"], {"blue": 300})
        await self.settle()
        maint = MaintenanceLights(self.live)
        await maint.hold_white(50)
        await self.settle()
        self.assertTrue(self.live.hold)
        await maint.release()
        await self.settle()
        self.assertFalse(self.live.hold)
        self.assertEqual(self.live.levels["prime_hd"]["blue"], 300)    # back to what you were holding
        self.assertEqual(self.hd16.calls[-1], ("live", 300, HOLD_SECONDS))

    async def test_maintenance_without_a_hold_goes_back_to_schedule(self):
        maint = MaintenanceLights(self.live)
        await maint.hold_white(50)
        await self.settle()
        await maint.release()
        self.assertFalse(self.live.active)
        self.assertEqual(self.hd.calls[-1], ("schedule",))


if __name__ == "__main__":
    unittest.main()
