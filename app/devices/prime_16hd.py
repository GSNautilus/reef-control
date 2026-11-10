"""Prime 16HD over Bluetooth LE with python-mobius.

- One connection at a time (the light allows one), shared through a lock,
  kept open while in use and dropped after IDLE_DISCONNECT seconds so the
  myAI app can connect again.
- The first connect on Windows sometimes fails, so every operation retries.
- Live control: the light ignores the LiveDemo attributes, but custom scenes
  work. Two scene slots are used in turn: write the idle slot, then start it,
  so the light switches with no blink (rewriting the *running* slot ends the
  scene). Every scene has a duration, so the light returns to its schedule
  by itself if the dashboard stops.
- Off: a third slot holds an all-zero scene with the longest duration the
  u16 field comfortably allows; "on" resumes the schedule.

Tested on 2026-11-04 (see PLAN.md for the details and the dead ends).
"""
from __future__ import annotations

import asyncio
import logging
import struct
from types import SimpleNamespace

from mobius.constants import C2Attribute, Model, VisualID
from mobius.device import MobiusDevice
from mobius.schedule import LightPrimitive, SchedulePoint

from .. import schedule as s

log = logging.getLogger(__name__)

CONNECT_TIMEOUT = 45
ATTEMPTS = 3
IDLE_DISCONNECT = 20
SCENE_SLOT_SIZE = 47
LIVE_SLOTS = [(1, 0x2B7E), (2, 0x2B7F)]  # (ConfiguredScenes index, scene id)
OFF_SLOT = (3, 0x2B80)
DASH_SLOTS = LIVE_SLOTS + [OFF_SLOT]
OFF_SECONDS = 60000
LIVE_NAME_PREFIX = b"dash "


class Prime16hdError(RuntimeError):
    pass


def _to_points(data: list[dict]) -> list[SchedulePoint]:
    return [SchedulePoint(p["time"], p["flags"], LightPrimitive({VisualID[k]: v for k, v in p["channels"].items()}))
            for p in data]


def _from_points(points: list[SchedulePoint]) -> list[dict]:
    return [{"time": p.time_minutes, "flags": p.flags,
             "channels": {vid.name: v for vid, v in p.light.channels.items()}} for p in points]


def _power_from(physical: dict) -> dict:
    """The light measures its own input: InputVoltage in mV, SupplyCurrent in mA
    (checked 2026-11-06: 25888 mV x 385 mA = 10.0 W mid-schedule, ~1.3 W idle)."""
    v, a = physical.get("InputVoltage"), physical.get("SupplyCurrent")
    if v is None or a is None:
        return {"watts": None}
    return {"watts": round(v / 1000 * a / 1000, 2), "volts": round(v / 1000, 2), "amps": round(a / 1000, 3)}


def _scene_slot(slot: int, scene_id: int, levels: dict[str, int], seconds: int) -> bytes:
    # Every channel is listed, zeros included: scene slots keep the full list
    # (checked 2026-11-04), unlike LiveDemoScene, which ends at the first 0.
    # Brightness must come first, or the light rejects the write.
    channels = {VisualID.Brightness: s.BRIGHTNESS_FULL}
    for ours, vid_name in s.VISUAL_IDS.items():
        channels[VisualID[vid_name]] = s.clamp(levels.get(ours, 0))
    name = (LIVE_NAME_PREFIX + str(slot).encode()).ljust(16, b"\x00")
    value = struct.pack("<HH", scene_id, seconds) + name + LightPrimitive(channels).encode()
    return value.ljust(SCENE_SLOT_SIZE, b"\x00")


class Prime16HD:
    name = "prime_16hd"

    def __init__(self, address: str, serial: str):
        self.address = address
        self.serial = serial
        self._dev: MobiusDevice | None = None
        self._lock = asyncio.Lock()
        self._idle_task: asyncio.Task | None = None
        self._next_slot = 0
        self._slots_checked = False
        self._channel_mw: dict[str, float] | None = None

    # ---- connection ---------------------------------------------------------

    async def _ensure_connected(self) -> MobiusDevice:
        if self._dev is not None and self._dev.is_connected:
            return self._dev
        self._dev = MobiusDevice(device=self.address, serial=self.serial, connect_timeout=CONNECT_TIMEOUT)
        await self._dev.connect()
        return self._dev

    async def _drop(self):
        dev, self._dev = self._dev, None
        if dev is not None:
            try:
                await dev.disconnect()
            except Exception:
                pass

    def _schedule_idle_disconnect(self):
        if self._idle_task:
            self._idle_task.cancel()

        async def later():
            await asyncio.sleep(IDLE_DISCONNECT)
            async with self._lock:
                await self._drop()

        self._idle_task = asyncio.create_task(later())

    async def _run(self, what: str, op):
        """Runs `op(dev)` under the lock, reconnecting and retrying on failure."""
        async with self._lock:
            error = None
            for attempt in range(1, ATTEMPTS + 1):
                try:
                    dev = await self._ensure_connected()
                    result = await op(dev)
                    self._schedule_idle_disconnect()
                    return result
                except Exception as e:
                    error = e
                    log.warning("16HD %s failed (attempt %d/%d): %r", what, attempt, ATTEMPTS, e)
                    await self._drop()
                    await asyncio.sleep(2)
            raise Prime16hdError(f"Prime 16HD {what} failed after {ATTEMPTS} tries: {error}")

    async def close(self):
        if self._idle_task:
            self._idle_task.cancel()
        async with self._lock:
            await self._drop()

    # ---- status & schedule --------------------------------------------------

    async def status(self) -> dict:
        async def op(dev):
            physical = await dev.get_extra_physical_values()
            # Read directly: get_current_scene() treats a timer above the
            # light's stored SceneTimeout as "ended", which hides the long off scene.
            scene_id = struct.unpack_from("<H", (await dev.get_attribute(C2Attribute.CurrentScene))[0])[0]
            timer = struct.unpack_from("<H", (await dev.get_attribute(C2Attribute.SceneTimer))[0])[0]
            scene = SimpleNamespace(id=scene_id, duration_seconds=timer) if scene_id and timer else None
            return {
                "online": True,
                "model": "Prime 16HD",
                "firmware": (await dev.get_firmware_versions(Model.PrimeSixteenHD)).get("Firmware"),
                "operation_state": (await dev.get_attribute(C2Attribute.OperationState))[0][0],
                "scene": {"id": scene.id, "seconds_left": scene.duration_seconds} if scene else None,
                "driver_temperature": physical.get("DriverTemperature"),
                "module_temperature": physical.get("ModuleTemperature"),
                **_power_from(physical),
            }
        return await self._run("status", op)

    async def power(self) -> dict:
        """Measured draw right now: {"watts", "volts", "amps"} (one quick read)."""
        async def op(dev):
            return _power_from(await dev.get_extra_physical_values())
        return await self._run("read power", op)

    async def channel_power_mw(self) -> dict[str, float]:
        """Each channel's rated draw at 100%, in mW, by VisualID name (NormalPower,
        or MaxPower if the light doesn't report NormalPower). Read once."""
        if self._channel_mw is None:
            async def op(dev):
                info = await dev.get_channel_power_info()
                rated = info.normal_power or info.max_power
                return {getattr(k, "name", str(k)): float(v) for k, v in rated.items()}
            self._channel_mw = await self._run("read channel power", op)
        return self._channel_mw

    async def get_schedule(self) -> list[dict]:
        async def op(dev):
            return _from_points(await dev.get_light_schedule(1))
        return await self._run("read schedule", op)

    async def set_schedule(self, data: list[dict]) -> None:
        points = _to_points(data)

        async def op(dev):
            await dev.set_light_schedule(points, 1)
        await self._run("write schedule", op)

    # ---- live control & off ---------------------------------------------------

    async def _check_slots(self, dev):
        """Refuses to use slots 1-3 if something other than the dashboard
        saved a scene there; clears live leftovers from an earlier run. The
        off slot, and a live slot whose scene is running right now (a held
        setting from before a dashboard restart), are left alone: rewriting a
        running scene's slot ends it."""
        running = struct.unpack_from("<H", (await dev.get_attribute(C2Attribute.CurrentScene))[0])[0]
        for i, (index, scene_id) in enumerate(DASH_SLOTS):
            raw = (await dev.get_attribute(C2Attribute.ConfiguredScenes, index=index))[0]
            if raw == bytes(len(raw)):
                continue
            ours = struct.unpack_from("<H", raw)[0] == scene_id and raw[4:9] == LIVE_NAME_PREFIX
            if not ours:
                raise Prime16hdError(f"scene slot {index} holds a scene the dashboard didn't create; "
                                     "the dashboard won't overwrite it")
            if (index, scene_id) in LIVE_SLOTS:
                if scene_id == running:
                    self._next_slot = 1 - i      # write the other live slot next
                else:
                    await dev.set_attribute(C2Attribute.ConfiguredScenes, bytes(SCENE_SLOT_SIZE), index=index)
        self._slots_checked = True

    async def _start_scene_in(self, dev, slot: tuple[int, int], levels: dict[str, int], seconds: int):
        if not self._slots_checked:
            await self._check_slots(dev)
        index, scene_id = slot
        await dev.set_attribute(C2Attribute.ConfiguredScenes, _scene_slot(index, scene_id, levels, seconds),
                                index=index)
        await dev.start_scene(scene_id, duration_seconds=seconds, broadcast=False)

    async def _clear(self, dev, slots):
        for index, _ in slots:
            await dev.set_attribute(C2Attribute.ConfiguredScenes, bytes(SCENE_SLOT_SIZE), index=index)

    async def set_live(self, levels: dict[str, int], seconds: int) -> None:
        async def op(dev):
            # Check first: after a restart that finds out which live slot is
            # running, so the *other* one gets written (no blink).
            if not self._slots_checked:
                await self._check_slots(dev)
            await self._start_scene_in(dev, LIVE_SLOTS[self._next_slot], levels, seconds)
            self._next_slot = 1 - self._next_slot
        await self._run("live update", op)

    async def stop_live(self, then_off: bool = False) -> None:
        """Ends live control: back to the schedule, or straight to the off
        scene (started before the live slots are cleared, so it doesn't
        flash the schedule)."""
        async def op(dev):
            if then_off:
                await self._start_scene_in(dev, OFF_SLOT, {}, OFF_SECONDS)
            else:
                await dev.resume_schedule(broadcast=False)
            await self._clear(dev, LIVE_SLOTS)
        await self._run("stop live", op)

    async def set_off(self) -> None:
        """Dark for OFF_SECONDS (~16.7 h, near the u16 limit): an all-zero
        scene. The dashboard renews it while it runs; if it isn't running,
        the light goes back to its schedule when the scene runs out."""
        async def op(dev):
            await self._start_scene_in(dev, OFF_SLOT, {}, OFF_SECONDS)
        await self._run("turn off", op)

    async def set_on(self) -> None:
        async def op(dev):
            await dev.resume_schedule(broadcast=False)
            await self._clear(dev, [OFF_SLOT])
        await self._run("turn on", op)
