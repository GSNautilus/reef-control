import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKUP_DIR = ROOT / "backups"
DATA_DIR = ROOT / "data"
WEB_DIR = ROOT / "web"


ROLES = ("return", "circulation", "skimmer", "ato")
PUMP_ROLES = ("return", "circulation")


@dataclass(frozen=True)
class Outlet:
    position: int
    role: str
    name: str


@dataclass(frozen=True)
class Config:
    port: int
    prime_hd_host: str
    prime_16hd_address: str
    prime_16hd_serial: str
    live_idle_timeout: int
    strip_host: str = ""
    strip_poll_seconds: int = 5
    outlets: tuple[Outlet, ...] = ()
    # Defaults for settings that can be changed on the page.
    defaults: dict = field(default_factory=dict)


def load(path: Path = ROOT / "config.toml") -> Config:
    if not path.exists():
        raise FileNotFoundError(f"{path} not found: copy config.example.toml to config.toml and fill in your devices")
    raw = tomllib.loads(path.read_text())
    strip = raw.get("strip", {})
    outlets = tuple(Outlet(int(o["position"]), o["role"], o["name"]) for o in strip.get("outlets", []))
    for o in outlets:
        if o.role not in ROLES:
            raise ValueError(f"config.toml: outlet {o.position} has unknown role {o.role!r} (use one of {ROLES})")
    modes, ato = raw.get("modes", {}), raw.get("ato", {})
    return Config(
        port=int(raw["server"]["port"]),
        prime_hd_host=raw["prime_hd"]["host"],
        prime_16hd_address=raw["prime_16hd"]["address"],
        prime_16hd_serial=raw["prime_16hd"]["serial"],
        live_idle_timeout=int(raw["live"]["idle_timeout_seconds"]),
        strip_host=strip.get("host", ""),
        strip_poll_seconds=int(strip.get("poll_seconds", 5)),
        outlets=outlets,
        defaults={
            "feed_minutes": int(modes.get("feed_minutes", 10)),
            "skimmer_delay_minutes": int(modes.get("skimmer_delay_minutes", 5)),
            "maintenance_white_percent": float(modes.get("maintenance_white_percent", 5)),
            "ato_long_run_seconds": int(ato.get("long_run_seconds", 120)),
            "ato_runs_factor": float(ato.get("runs_factor", 2.0)),
        },
    )
