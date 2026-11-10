"""Reef Control web server: http://localhost:<port> on this PC only."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, services, store
from . import schedule as s
from .devices.p316m import P316M
from .energy import EnergyService
from .outlets import StripService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("reef")

cfg = config.load()


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.lights = services.Lights(cfg)
    app.state.power = services.Power(app.state.lights)
    app.state.live = services.LiveControl(app.state.lights, cfg.live_idle_timeout, app.state.power)
    app.state.power.live = app.state.live
    app.state.power.start()
    app.state.restore_live = asyncio.create_task(app.state.live.restore())  # a setting held before a restart
    app.state.strip = None
    if cfg.strip_host and cfg.outlets:
        app.state.strip = StripService(cfg, P316M(cfg.strip_host), services.MaintenanceLights(app.state.live))
        app.state.strip.start()
    app.state.energy = EnergyService(cfg, app.state.strip, app.state.lights.hd, app.state.lights.hd16)
    app.state.energy.start()
    yield
    app.state.power.close()
    await app.state.energy.close()
    if app.state.strip:
        await app.state.strip.close()
    try:
        await app.state.live.close()  # a held setting stays on the lights (re-applied at the next start)
    finally:
        await app.state.lights.close()


app = FastAPI(title="Reef Control", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=config.WEB_DIR), name="static")


def light_or_404(light: str) -> str:
    if light not in s.LIGHTS:
        raise HTTPException(404, f"unknown light {light!r}")
    return light


class Draft(BaseModel):
    points: list[dict]
    tweaks: dict[str, dict] = Field(default_factory=dict)
    shift: int = 0  # minutes; negative = earlier


class DimRequest(BaseModel):
    on: bool
    percent: int


class LiveRequest(BaseModel):
    targets: list[str]
    levels: dict[str, int]


class LiveStop(BaseModel):
    targets: list[str] | None = None


class PowerRequest(BaseModel):
    targets: list[str]
    on: bool


@app.get("/")
async def index():
    return FileResponse(config.WEB_DIR / "index.html")


@app.get("/api/status")
async def status(refresh: bool = False):
    return {**await app.state.lights.status(refresh_16hd=refresh), "power": app.state.power.snapshot(),
            "dim": app.state.lights.dim_snapshot()}


@app.get("/api/power")
async def power_state():
    return app.state.power.snapshot()


@app.post("/api/power")
async def power_set(req: PowerRequest):
    for t in req.targets:
        light_or_404(t)
    return await app.state.power.set(req.targets, req.on)


@app.get("/api/draft")
async def get_draft():
    draft = store.get_draft()
    if draft is None:
        try:
            points = await app.state.lights.import_schedule("prime_hd")
            draft = {**services.default_draft(), "points": [p.to_json() for p in points]}
        except Exception as e:
            log.warning("couldn't import the Prime HD schedule for a first draft: %s", e)
            draft = services.default_draft()
        store.put_draft(draft)
    return services.draft_view(draft, services.current_dim())


@app.put("/api/draft")
async def put_draft(draft: Draft):
    data = services.clean_draft(draft.model_dump())
    store.put_draft(data)
    return services.draft_view(data, services.current_dim())


@app.post("/api/draft/import/{light}")
async def import_draft(light: str):
    points = await app.state.lights.import_schedule(light_or_404(light))
    draft = {**services.clean_draft(store.get_draft() or services.default_draft()),
             "points": [p.to_json() for p in points]}
    store.put_draft(draft)
    return services.draft_view(draft, services.current_dim())


@app.post("/api/send")
async def send():
    draft = store.get_draft()
    if draft is None:
        raise HTTPException(400, "no draft to send")
    if app.state.live.active:
        raise HTTPException(409, "stop live control before sending a schedule")
    try:
        out = await app.state.lights.send(draft)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except services.Busy as e:
        raise HTTPException(409, str(e))
    await app.state.power.reassert_off()
    return out


@app.post("/api/dim")
async def dim_set(req: DimRequest):
    if app.state.live.active:
        raise HTTPException(409, "live control is holding the lights; press \"Back to schedule\" "
                                 "(or finish maintenance) before changing dim mode")
    try:
        out = await app.state.lights.set_dim(req.on, req.percent)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except services.Busy as e:
        raise HTTPException(409, str(e))
    if out["results"]:
        await app.state.power.reassert_off()
    return out


@app.get("/api/history")
async def history():
    return store.list_history()


@app.post("/api/history/{entry_id}/load")
async def load_history(entry_id: int):
    entry = store.get_history(entry_id)
    if entry is None:
        raise HTTPException(404, "no such history entry")
    draft = services.clean_draft(entry["draft"])
    store.put_draft(draft)
    return services.draft_view(draft, services.current_dim())


@app.get("/api/live")
async def live_state():
    return app.state.live.snapshot()


@app.post("/api/live")
async def live_set(req: LiveRequest):
    for t in req.targets:
        light_or_404(t)
    return await app.state.live.set(req.targets, req.levels)


@app.post("/api/live/stop")
async def live_stop(req: LiveStop):
    return await app.state.live.stop(req.targets)


# ---- power strip ---------------------------------------------------------------------

class OutletRequest(BaseModel):
    action: str                 # "on", "off_for" (with minutes), "off" (skimmer/ATO only)
    minutes: float | None = None


class ModeRequest(BaseModel):
    action: str                 # feed: "start"/"end"; maintenance: "start"/"done"


class NameRequest(BaseModel):
    name: str


def strip() -> StripService:
    if app.state.strip is None:
        raise HTTPException(404, "no power strip configured in config.toml")
    return app.state.strip


async def strip_call(coro):
    try:
        return await coro
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(503, str(e))


@app.get("/api/strip")
async def strip_state():
    return strip().snapshot()


@app.post("/api/strip/outlet/{position}")
async def strip_outlet(position: int, req: OutletRequest):
    return await strip_call(strip().outlet(position, req.action, req.minutes))


@app.post("/api/strip/feed")
async def strip_feed(req: ModeRequest):
    return await strip_call(strip().feed(req.action))


@app.post("/api/strip/maintenance")
async def strip_maintenance(req: ModeRequest):
    return await strip_call(strip().maintenance(req.action))


@app.put("/api/strip/settings")
async def strip_settings(changes: dict):
    try:
        return strip().update_settings(changes)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.put("/api/strip/outlet/{position}/name")
async def strip_rename(position: int, req: NameRequest):
    try:
        return strip().rename(position, req.name)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/strip/outlet/{position}/history")
async def strip_outlet_history(position: int, hours: float = 24):
    try:
        return strip().outlet_history(position, hours)
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.get("/api/strip/ato/history")
async def strip_ato_history(days: int = 14):
    return strip().ato_history(days)


@app.get("/api/energy/summary")
async def energy_summary(span: str = "week"):
    if span not in ("day", "week", "month", "year"):
        raise HTTPException(400, "span must be day, week, month or year")
    return app.state.energy.summary(span)


@app.get("/api/energy/trends")
async def energy_trends():
    return app.state.energy.trends()


@app.post("/api/strip/outlet/{position}/relearn")
async def strip_relearn(position: int):
    try:
        return strip().relearn_normal(position)
    except ValueError as e:
        raise HTTPException(400, str(e))
