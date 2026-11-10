"""Tapo P316M power strip over its local API, with python-kasa.

The strip was paired over Matter from this PC (no TP-Link app, no account)
and answers python-kasa with its factory default login. Everything here is
local. See PLAN.md "What's proven -- power strip".

python-kasa covers on/off, energy and the clock. Countdown timers and the
power-cut behavior aren't in python-kasa, so they're raw Tapo requests
(formats proven on the real strip, 2026-11-06).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from kasa import Credentials, Discover
from kasa.exceptions import KasaException

log = logging.getLogger(__name__)

LOCAL_TZ = ZoneInfo("America/New_York")


class StripError(RuntimeError):
    pass


class P316M:
    def __init__(self, host: str):
        self.host = host
        self._dev = None
        self._lock = asyncio.Lock()

    async def close(self):
        async with self._lock:
            await self._drop()

    async def _drop(self):
        if self._dev is not None:
            try:
                await self._dev.disconnect()
            except Exception:
                pass
            self._dev = None

    async def _device(self):
        if self._dev is None:
            # A blank login makes python-kasa fall back to the factory default one.
            dev = await Discover.discover_single(self.host, credentials=Credentials(), discovery_timeout=3)
            if dev is None:
                raise StripError(f"nothing answered at {self.host}")
            self._dev = dev
        return self._dev

    async def _run(self, fn, update: bool = True):
        """Runs fn(device) under the lock, reconnecting once if the session broke."""
        async with self._lock:
            for attempt in (1, 2):
                try:
                    dev = await self._device()
                    if update:
                        await dev.update()
                    return await fn(dev)
                except (KasaException, OSError, asyncio.TimeoutError) as e:
                    await self._drop()
                    if attempt == 2:
                        raise StripError(f"power strip at {self.host}: {e}") from e
                    log.info("power strip connection dropped (%s), reconnecting", e)

    @staticmethod
    def _outlets(dev) -> dict:
        return {c._info.get("position"): c for c in dev.children}

    @staticmethod
    async def _raw(target, method: str, params=None):
        resp = await target.protocol.query({method: params})
        return resp.get(method, resp)

    # ---- reading ---------------------------------------------------------------

    async def read(self) -> dict:
        """Every outlet's state and power, plus the strip's clock."""
        async def fn(dev):
            outlets = {}
            for pos, c in self._outlets(dev).items():
                info = c._info
                em = c.modules.get("Energy")
                default = info.get("default_states") or {}
                outlets[pos] = {
                    "on": bool(c.is_on),
                    "watts": None if em is None else em.current_consumption,
                    "volts": None if em is None else em.voltage,
                    "amps": None if em is None else em.current,
                    "today_kwh": None if em is None else em.consumption_today,
                    "on_time": info.get("on_time"),
                    "default_on": (default.get("state") or {}).get("on") if default.get("type") == "custom" else None,
                }
            t = dev.modules.get("Time")
            clock = t.time if t else None
            return {"outlets": outlets, "clock": clock.isoformat() if clock else None,
                    "firmware": dev._info.get("fw_ver")}
        return await self._run(fn)

    async def countdowns(self) -> dict:
        """{position: {"on": bool, "remain": seconds}} for outlets with an active countdown."""
        async def fn(dev):
            out = {}
            for pos, c in self._outlets(dev).items():
                rules = await self._raw(c, "get_countdown_rules", {"start_index": 0})
                for r in (rules or {}).get("rule_list", []):
                    if r.get("enable"):
                        out[pos] = {"on": r["desired_states"]["on"], "remain": r.get("remain", 0)}
            return out
        return await self._run(fn)  # the update loads the outlets on a fresh connection

    async def energy_data(self, requests: list[tuple[int, int, int, int]]) -> list[list[int] | None]:
        """The strip's own stored energy, in whole Wh per period, for several
        (outlet, start, end, interval) requests in one connection. interval 60
        = per hour (whole days), 1440 = per day (within one calendar quarter),
        43200 = per month (within one calendar year). start/end are local-time
        epoch seconds; each list starts at `start`. A refused request gives None."""
        async def fn(dev):
            outs, results = self._outlets(dev), []
            for pos, start, end, interval in requests:
                try:
                    r = await self._raw(outs[pos], "get_energy_data",
                                        {"start_timestamp": int(start), "end_timestamp": int(end), "interval": interval})
                    results.append([int(v) for v in (r or {}).get("data", [])])
                except KasaException as e:
                    log.info("strip energy request %s refused: %s", (pos, start, end, interval), e)
                    results.append(None)
            return results
        return await self._run(fn)

    # ---- switching -------------------------------------------------------------

    async def _set_countdown(self, child, seconds: int, on: bool):
        rules = await self._raw(child, "get_countdown_rules", {"start_index": 0})
        rule = {"delay": int(seconds), "desired_states": {"on": on}, "enable": True, "remain": int(seconds)}
        existing = (rules or {}).get("rule_list") or []
        if existing:
            await self._raw(child, "edit_countdown_rule", {"id": existing[0]["id"], **rule})
        else:
            await self._raw(child, "add_countdown_rule", rule)

    async def _cancel_countdown(self, child):
        rules = await self._raw(child, "get_countdown_rules", {"start_index": 0})
        for r in (rules or {}).get("rule_list") or []:
            if r.get("enable"):
                await self._raw(child, "edit_countdown_rule",
                                {"id": r["id"], "enable": False, "delay": 0, "remain": 0,
                                 "desired_states": r.get("desired_states", {"on": True})})

    async def switch(self, changes: dict[int, bool | tuple[bool, int]]):
        """Sets outlets in one go. A value is on/off (any countdown is cancelled),
        or (False, seconds): off now, and the strip turns it back on by itself
        after `seconds`, even if this PC is off."""
        async def fn(dev):
            outs = self._outlets(dev)
            for pos, change in changes.items():
                c = outs[pos]
                if isinstance(change, tuple):
                    on, seconds = change
                    if on:
                        raise ValueError("a timed change only goes off, then back on")
                    await c.turn_off()
                    await self._set_countdown(c, seconds, True)
                else:
                    await self._cancel_countdown(c)
                    await (c.turn_on() if change else c.turn_off())
        await self._run(fn, update=True)

    async def countdown_on(self, pos: int, seconds: int):
        """Leaves the outlet as it is (off) and has the strip turn it on after `seconds`."""
        async def fn(dev):
            await self._set_countdown(self._outlets(dev)[pos], seconds, True)
        await self._run(fn, update=True)

    # ---- settings kept on the strip ------------------------------------------------

    async def ensure_settings(self, power_cut_on: dict[int, bool]) -> list[str]:
        """Power-cut behavior per outlet, automatic firmware updates off. Only
        writes what differs; returns what it changed."""
        async def fn(dev):
            changed = []
            for pos, c in self._outlets(dev).items():
                if pos not in power_cut_on:
                    continue
                want = {"type": "custom", "state": {"on": power_cut_on[pos]}}
                have = c._info.get("default_states") or {}
                if have.get("type") != "custom" or (have.get("state") or {}).get("on") != power_cut_on[pos]:
                    await self._raw(c, "set_device_info", {"default_states": want})
                    changed.append(f"outlet {pos} after a power cut: {'on' if power_cut_on[pos] else 'off'}")
            au = await self._raw(dev, "get_auto_update_info")
            if isinstance(au, dict) and au.get("enable"):
                await self._raw(dev, "set_auto_update_info", {**au, "enable": False})
                changed.append("automatic firmware updates: off")
            return changed
        return await self._run(fn, update=True)

    async def set_clock(self):
        async def fn(dev):
            t = dev.modules.get("Time")
            if t is not None:
                await t.set_time(datetime.now(LOCAL_TZ))
        await self._run(fn, update=True)
