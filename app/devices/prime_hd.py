"""Prime HD over its local HTTP API -- the same calls its own web UI makes
(found in the light's js/ai-prime-comm.js).

Levels are 0-1000 per color. A `POST /api/colors` while the schedule is on
is a preview: the light returns to its schedule by itself after ~6-8 s, so
live control re-sends it as a keep-alive and never turns the schedule off.
"""
from __future__ import annotations

import httpx

from .. import schedule as s

TIMEOUT = httpx.Timeout(5.0)


class PrimeHdError(RuntimeError):
    pass


class PrimeHD:
    name = "prime_hd"

    def __init__(self, host: str):
        self.base = f"http://{host}/api"
        self._client = httpx.AsyncClient(timeout=TIMEOUT)
        self._full_mw: dict[str, float] | None = None  # each color's draw at 100%

    async def close(self):
        await self._client.aclose()

    async def _call(self, method: str, path: str, body=None) -> dict:
        try:
            resp = await self._client.request(method, f"{self.base}/{path}", json=body)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            raise PrimeHdError(f"Prime HD {method} /api/{path} failed: {e}") from e
        if data.get("response_code", 0) != 0:
            raise PrimeHdError(f"Prime HD {method} /api/{path} returned response_code {data['response_code']}")
        return data

    async def status(self) -> dict:
        identity = await self._call("GET", "identity")
        colors = await self._call("GET", "colors")
        enabled = await self._call("GET", "schedule/enable")
        return {
            "online": True,
            "model": identity.get("product"),
            "firmware": identity.get("firmware"),
            "schedule_enabled": enabled.get("enable"),
            "levels": {ours: colors.get(hd, 0) for hd, ours in s.HD_COLORS.items()},
            "watts_estimate": await self.watts_estimate(colors),
        }

    async def current_watts(self) -> float | None:
        """Estimated draw right now (one request)."""
        return await self.watts_estimate(await self._call("GET", "colors"))

    async def full_power_mw(self) -> dict[str, float] | None:
        """Each color's draw at 100%, in mW (from `/api/power`)."""
        await self.watts_estimate({})
        return self._full_mw

    async def watts_estimate(self, colors: dict) -> float | None:
        """LED power from the light's own figures: `/api/power` gives each
        color's draw at 100% ("normal", in mW; 48 W in total). The driver and
        fan add a little on top, so this is an estimate."""
        if self._full_mw is None:
            try:
                dev = (await self._call("GET", "power"))["devices"][0]
                self._full_mw = {k: float(v) for k, v in dev["normal"].items()}
            except (PrimeHdError, KeyError, IndexError, TypeError, ValueError):
                return None
        mw = sum(min(colors.get(c, 0), s.MAX_LEVEL) / s.MAX_LEVEL * full for c, full in self._full_mw.items())
        return round(mw / 1000, 1)

    async def get_schedule(self) -> dict:
        data = await self._call("GET", "schedule")
        return {"name": data.get("name", "default"), "ramps": data["ramps"]}

    async def set_schedule(self, payload: dict) -> None:
        await self._call("POST", "schedule", payload)

    async def set_levels(self, levels: dict[str, int]) -> None:
        await self._call("POST", "colors", {hd: s.clamp(levels.get(ours, 0)) for hd, ours in s.HD_COLORS.items()})

    async def set_manual(self, levels: dict[str, int]) -> None:
        """Manual mode at these levels: the light holds them by itself, PC or
        not, until the schedule is turned back on (resume_schedule)."""
        await self._call("PUT", "schedule/enable", {"enable": False})
        await self.set_levels(levels)

    async def set_off(self) -> None:
        """Manual mode at zero: the light stays dark, PC or not, until
        the schedule is turned back on."""
        await self._call("PUT", "schedule/enable", {"enable": False})
        await self.set_levels({})

    async def resume_schedule(self) -> None:
        """Ends a preview now instead of after ~8 s. Turning the schedule off
        and on again is what reverted the light immediately in testing."""
        await self._call("PUT", "schedule/enable", {"enable": False})
        await self._call("PUT", "schedule/enable", {"enable": True})
