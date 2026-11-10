"""SQLite storage: the schedule draft and the history of schedules sent."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from .config import DATA_DIR

DB_PATH = DATA_DIR / "reef.db"


@contextmanager
def _connect():
    """A connection that commits on success and is always closed (sqlite3's own
    `with` only commits, which leaves the file open on Windows)."""
    db = _open()
    try:
        with db:
            yield db
    finally:
        db.close()


def _open() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    db.execute("""CREATE TABLE IF NOT EXISTS history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created TEXT NOT NULL,
        draft TEXT NOT NULL,
        results TEXT NOT NULL)""")
    # One row per outlet per minute: average watts and whether it was on.
    db.execute("""CREATE TABLE IF NOT EXISTS outlet_minutes (
        minute INTEGER NOT NULL,
        outlet INTEGER NOT NULL,
        watts REAL,
        is_on INTEGER NOT NULL,
        PRIMARY KEY (minute, outlet))""")
    db.execute("""CREATE TABLE IF NOT EXISTS ato_runs (
        started REAL PRIMARY KEY,
        seconds REAL NOT NULL,
        peak_watts REAL)""")
    # Energy per hour ('H'), day ('D') or month ('M'), starting at local time `start`.
    # device: 'outlet:N' or 'light:prime_hd'. source: 'strip' (the strip's own
    # meter), 'log' (the dashboard's per-minute readings), 'schedule' (estimated
    # from the light's schedule for times the dashboard wasn't running).
    db.execute("""CREATE TABLE IF NOT EXISTS energy (
        period TEXT NOT NULL,
        start INTEGER NOT NULL,
        device TEXT NOT NULL,
        wh REAL NOT NULL,
        source TEXT NOT NULL,
        PRIMARY KEY (period, start, device))""")
    db.execute("""CREATE TABLE IF NOT EXISTS light_minutes (
        minute INTEGER NOT NULL,
        light TEXT NOT NULL,
        watts REAL NOT NULL,
        PRIMARY KEY (minute, light))""")
    # Per outlet per day, kept forever for trends: minutes logged, minutes on,
    # average draw while on.
    db.execute("""CREATE TABLE IF NOT EXISTS outlet_days (
        day TEXT NOT NULL,
        outlet INTEGER NOT NULL,
        logged_minutes INTEGER NOT NULL,
        on_minutes INTEGER NOT NULL,
        avg_on_watts REAL,
        PRIMARY KEY (day, outlet))""")
    return db


def _get(key: str):
    with _connect() as db:
        row = db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else None


def _put(key: str, value) -> None:
    with _connect() as db:
        db.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, json.dumps(value)))


def get(key: str, default=None):
    value = _get(key)
    return default if value is None else value


def put(key: str, value) -> None:
    _put(key, value)


def add_outlet_minutes(rows: list[tuple[int, int, float | None, bool]]) -> None:
    """rows: (minute epoch, outlet, average watts, on)."""
    with _connect() as db:
        db.executemany("INSERT OR REPLACE INTO outlet_minutes VALUES (?, ?, ?, ?)",
                       [(m, o, w, int(on)) for m, o, w, on in rows])


def outlet_minutes(outlet: int, since_minute: int) -> list[tuple[int, float | None, bool]]:
    with _connect() as db:
        rows = db.execute("SELECT minute, watts, is_on FROM outlet_minutes WHERE outlet = ? AND minute >= ? "
                          "ORDER BY minute", (outlet, since_minute)).fetchall()
    return [(r["minute"], r["watts"], bool(r["is_on"])) for r in rows]


def add_ato_run(started: float, seconds: float, peak_watts: float | None) -> None:
    with _connect() as db:
        db.execute("INSERT OR REPLACE INTO ato_runs VALUES (?, ?, ?)", (started, seconds, peak_watts))


def ato_runs(since: float) -> list[dict]:
    with _connect() as db:
        rows = db.execute("SELECT * FROM ato_runs WHERE started >= ? ORDER BY started", (since,)).fetchall()
    return [{"started": r["started"], "seconds": r["seconds"], "peak_watts": r["peak_watts"]} for r in rows]


def prune(before_minute: int) -> None:
    """Drops per-minute detail older than `before_minute`. ATO runs, energy
    totals and daily outlet figures are small and kept forever."""
    with _connect() as db:
        db.execute("DELETE FROM outlet_minutes WHERE minute < ?", (before_minute,))
        db.execute("DELETE FROM light_minutes WHERE minute < ?", (before_minute,))


def put_energy(rows: list[tuple[str, int, str, float, str]]) -> None:
    """rows: (period, start, device, wh, source)."""
    with _connect() as db:
        db.executemany("INSERT OR REPLACE INTO energy VALUES (?, ?, ?, ?, ?)", rows)


def energy(period: str, since: int, until: int) -> list[dict]:
    with _connect() as db:
        rows = db.execute("SELECT * FROM energy WHERE period = ? AND start >= ? AND start < ? ORDER BY start",
                          (period, since, until)).fetchall()
    return [dict(r) for r in rows]


def outlet_hour_sums(since_minute: int, until_minute: int) -> dict[tuple[int, int], tuple[int, float]]:
    """{(outlet, hour start epoch): (minutes with a reading, sum of watts)} from the minute log."""
    with _connect() as db:
        rows = db.execute("SELECT outlet, minute / 60 AS h, COUNT(watts) AS n, SUM(watts) AS s FROM outlet_minutes "
                          "WHERE minute >= ? AND minute < ? AND watts IS NOT NULL GROUP BY outlet, h",
                          (since_minute, until_minute)).fetchall()
    return {(r["outlet"], r["h"] * 3600): (r["n"], r["s"] or 0.0) for r in rows}


def light_hour_sums(light: str, since_minute: int, until_minute: int) -> dict[int, tuple[int, float]]:
    """{hour start epoch: (minutes logged, sum of watts)} for a light's minute log."""
    with _connect() as db:
        rows = db.execute("SELECT minute / 60 AS h, COUNT(*) AS n, SUM(watts) AS s FROM light_minutes "
                          "WHERE light = ? AND minute >= ? AND minute < ? GROUP BY h",
                          (light, since_minute, until_minute)).fetchall()
    return {r["h"] * 3600: (r["n"], r["s"] or 0.0) for r in rows}


def add_light_minute(minute: int, light: str, watts: float) -> None:
    with _connect() as db:
        db.execute("INSERT OR REPLACE INTO light_minutes VALUES (?, ?, ?)", (minute, light, watts))


def light_minutes(light: str, since_minute: int, until_minute: int) -> list[tuple[int, float]]:
    with _connect() as db:
        rows = db.execute("SELECT minute, watts FROM light_minutes WHERE light = ? AND minute >= ? AND minute < ? "
                          "ORDER BY minute", (light, since_minute, until_minute)).fetchall()
    return [(r["minute"], r["watts"]) for r in rows]


def put_outlet_days(rows: list[tuple[str, int, int, int, float | None]]) -> None:
    """rows: (day 'YYYY-MM-DD', outlet, logged_minutes, on_minutes, avg_on_watts)."""
    with _connect() as db:
        db.executemany("INSERT OR REPLACE INTO outlet_days VALUES (?, ?, ?, ?, ?)", rows)


def outlet_days(since_day: str) -> list[dict]:
    with _connect() as db:
        rows = db.execute("SELECT * FROM outlet_days WHERE day >= ? ORDER BY day", (since_day,)).fetchall()
    return [dict(r) for r in rows]


def get_draft() -> dict | None:
    return _get("draft")


def put_draft(draft: dict) -> None:
    _put("draft", draft)


def get_power() -> dict | None:
    """{light: True (following its schedule) / False (held off)}."""
    return _get("power")


def put_power(power: dict) -> None:
    _put("power", power)


def add_history(draft: dict, results: dict) -> int:
    with _connect() as db:
        cur = db.execute("INSERT INTO history (created, draft, results) VALUES (?, ?, ?)",
                         (datetime.now().isoformat(timespec="seconds"), json.dumps(draft), json.dumps(results)))
        return cur.lastrowid


def list_history(limit: int = 50) -> list[dict]:
    with _connect() as db:
        rows = db.execute("SELECT * FROM history ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [{"id": r["id"], "created": r["created"], "draft": json.loads(r["draft"]),
             "results": json.loads(r["results"])} for r in rows]


def get_history(entry_id: int) -> dict | None:
    with _connect() as db:
        row = db.execute("SELECT * FROM history WHERE id = ?", (entry_id,)).fetchone()
    return {"id": row["id"], "draft": json.loads(row["draft"])} if row else None
