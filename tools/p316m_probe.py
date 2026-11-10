"""Test the Tapo P316M strip with no TP-Link app and no account.

Steps, in order (see the docstring of each command):

  wifi     Read-only. List the Wi-Fi networks this PC can see and flag any
           setup network the strip broadcasts.
  probe    Read-only. Find the strip, report its firmware and login type, and
           read everything (outlets, power, timers, schedules, clock). Saves a
           dump in backups\\.
  join     WRITES. Puts a factory-fresh strip on your home Wi-Fi using its
           setup network. It also gives the strip a local login that this
           project makes up and keeps in data\\p316m_login.json (it's not a
           TP-Link account and never touches the internet).
  test     WRITES, but refuses to run unless every outlet draws ~0 W (strip
           empty). Switches each outlet off and on, tests a "back on in N
           seconds" timer stored on the strip with the PC disconnected, sets
           the clock, and turns automatic firmware updates off.

Run from the project folder with the codex Python, e.g.
  python tools\\p316m_probe.py probe
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import re
import secrets
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from kasa import Credentials, Discover
from kasa.exceptions import AuthenticationError, KasaException, UnsupportedDeviceError

ROOT = Path(__file__).resolve().parent.parent
LOGIN_FILE = ROOT / "data" / "p316m_login.json"
BACKUPS = ROOT / "backups"
LOCAL_TZ = "America/New_York"
SUPPORTED_LOGINS = {"KLAP", "AES"}  # python-kasa 0.12.0; TPAP is not supported yet
EMPTY_WATTS = 0.5  # "test" refuses to run if any outlet draws more than this


# --------------------------------------------------------------------------- helpers


class _CredLog(logging.Handler):
    """Notes which login python-kasa fell back to (factory default / blank)."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.notes: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        if "default credentials worked" in msg:
            self.notes.append("factory default login (" + msg.rsplit("with ", 1)[-1] + ")")
        elif "blank credentials worked" in msg:
            self.notes.append("blank login")
        elif "match with expected credentials" in msg:
            self.notes.append("this project's login (data\\p316m_login.json)")


CRED_LOG = _CredLog()
_klap_log = logging.getLogger("kasa.transports.klaptransport")
_klap_log.setLevel(logging.DEBUG)
_klap_log.addHandler(CRED_LOG)
_klap_log.propagate = False


def load_login() -> Credentials | None:
    if LOGIN_FILE.exists():
        data = json.loads(LOGIN_FILE.read_text(encoding="utf-8"))
        return Credentials(data["username"], data["password"])
    return None


def make_login() -> Credentials:
    """Create (once) the strip's local login. Not a TP-Link account."""
    existing = load_login()
    if existing:
        return existing
    creds = Credentials("reef-control@local", secrets.token_urlsafe(18))
    LOGIN_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOGIN_FILE.write_text(
        json.dumps({"username": creds.username, "password": creds.password}, indent=2),
        encoding="utf-8",
    )
    print(f"Saved the strip's local login to {LOGIN_FILE}")
    return creds


def login_type(disc: dict) -> str:
    """Login/encryption type from a raw discovery result."""
    r = disc.get("result", disc)
    schm = r.get("mgt_encrypt_schm") or {}
    if schm.get("encrypt_type"):
        return str(schm["encrypt_type"])
    et = r.get("encrypt_type")
    if isinstance(et, list) and et:
        return ",".join(et)
    return str(et or "unknown")


def show_discovery(ip: str, disc: dict) -> None:
    r = disc.get("result", disc)
    lt = login_type(disc)
    print(f"\n=== {r.get('device_model', '?')} at {ip}")
    print(f"  MAC:             {r.get('mac', '?')}   <- use this to block its internet in the router")
    print(f"  Firmware:        {r.get('firmware_version') or '(not in discovery reply; read after login)'}")
    print(f"  Hardware:        {r.get('hw_ver') or '?'}")
    print(f"  Factory default: {r.get('factory_default')}   (True = never set up)")
    print(f"  Set up by:       {r.get('obd_src') or 'nobody'}   Owner set: {bool(r.get('owner'))}")
    print(f"  Login type:      {lt}")
    if lt in SUPPORTED_LOGINS:
        print("  -> python-kasa can talk to this login type.  GOOD")
    elif "TPAP" in lt:
        print("  -> TPAP: python-kasa 0.12.0 can NOT talk to this yet.  FAIL (for now)")
    else:
        print("  -> Unknown login type; the connect step below will tell.")


async def find(host: str | None, target: str) -> dict[str, dict]:
    """Raw discovery replies by IP (broadcast, or one host)."""
    found: dict[str, dict] = {}

    def raw(d) -> None:
        found[d["meta"]["ip"]] = d["discovery_response"]

    async def unsupported(err: UnsupportedDeviceError) -> None:
        if err.host and err.discovery_result:
            found.setdefault(err.host, err.discovery_result)

    if host:
        try:
            await Discover.discover_single(
                host, on_discovered_raw=raw, on_unsupported=unsupported, discovery_timeout=5
            )
        except UnsupportedDeviceError as err:
            if err.discovery_result:
                found.setdefault(host, err.discovery_result)
        except KasaException as err:
            print(f"(connect after discovery failed: {err})")
    else:
        await Discover.discover(
            target=target, on_discovered_raw=raw, on_unsupported=unsupported, discovery_timeout=5
        )
    return found


async def connect(host: str):
    """Discover + log in + update. Tries this project's login, then factory default."""
    CRED_LOG.notes.clear()
    dev = await Discover.discover_single(host, credentials=load_login() or Credentials())
    if dev is None:
        raise KasaException(f"No TP-Link device answered at {host}")
    await dev.update()
    return dev


async def raw(target, method: str, params=None):
    """One raw Tapo request on the strip or one outlet; returns result or error text."""
    try:
        resp = await target.protocol.query({method: params})
        return resp.get(method, resp)
    except KasaException as err:
        return {"error": str(err)}


def outlets(dev) -> list:
    return sorted(dev.children, key=lambda c: c._info.get("position", 0))


def watts(child) -> float | None:
    em = child.modules.get("Energy")
    return None if em is None else em.current_consumption


def wifi_gateway() -> str:
    """Gateway of this PC's Wi-Fi adapter = the strip while the PC is on its setup network.

    The PC is also on Ethernet, so a broadcast may never reach the Wi-Fi side.
    """
    out = subprocess.run(["ipconfig"], capture_output=True, text=True, errors="replace").stdout
    sections = re.split(r"\r?\n(?=\S)", out)
    for sec in sections:
        if sec.lower().startswith("wireless lan adapter"):
            m = re.search(r"Default Gateway[ .]*:(?:[^\n]*\n\s+)*?\s*(\d+\.\d+\.\d+\.\d+)", sec)
            if m:
                return m.group(1)
    return "192.168.0.1"


async def pick_host(host: str | None, target: str) -> str:
    if host:
        return host
    found = await find(None, target)
    strips = [ip for ip, d in found.items() if "P316" in str(d.get("result", d).get("device_model", ""))]
    if len(strips) == 1:
        return strips[0]
    if not found:
        sys.exit("No TP-Link device answered. Is the strip on this network? Try --host.")
    sys.exit(f"Found {list(found)} but not exactly one P316M; pass --host.")


# --------------------------------------------------------------------------- commands


def cmd_wifi(_args) -> None:
    """Read-only: list visible Wi-Fi networks, flag the strip's setup network."""
    out = subprocess.run(
        ["netsh", "wlan", "show", "networks", "mode=bssid"],
        capture_output=True, text=True, errors="replace",
    ).stdout
    ssids = re.findall(r"^SSID \d+ : (.*)$", out, flags=re.M)
    if not ssids:
        if "location permission" in out.lower():
            print("Windows won't list Wi-Fi networks to programs unless Location access is on\n"
                  "(Settings > Privacy & security > Location). No need to change that: just open\n"
                  "the Wi-Fi list from the taskbar and look for a network with 'Tapo' in its name.")
        else:
            print(out.strip() or "netsh returned nothing (is Wi-Fi on?)")
        return
    print("Wi-Fi networks this PC can see:")
    hits = []
    for s in ssids:
        flag = bool(re.search(r"tapo|tp-?link|p316", s, flags=re.I))
        hits += [s] if flag else []
        print(f"  {'>>' if flag else '  '} {s or '(hidden)'}")
    if hits:
        print(f"\nLikely the strip's setup network: {hits}. Connect the PC to it, then run: probe")
    else:
        print("\nNo Tapo/TP-Link setup network seen. Unplug the strip, plug it back in, wait a"
              " minute and run this again. If it never shows, the strip only sets up over"
              " Bluetooth and the Wi-Fi route fails.")


async def cmd_probe(args) -> None:
    """Read-only: find the strip, report login type, read everything, save a dump."""
    found = await find(args.host, args.target)
    if not found and not args.host:
        gw = wifi_gateway()
        print(f"Nothing answered the broadcast; trying the Wi-Fi gateway {gw} (the strip, if this"
              " PC is on its setup network) ...")
        found = await find(gw, args.target)
        args.host = gw if found else None
    if not found:
        sys.exit("No TP-Link device answered. Is the strip plugged in, and is the PC on its setup"
                 " network or on the same network as the strip?")
    for ip, disc in found.items():
        show_discovery(ip, disc)

    host = args.host or next(
        (ip for ip, d in found.items() if "P316" in str(d.get("result", d).get("device_model", ""))),
        None,
    )
    if not host:
        print("\nNo P316M among them; stopping.")
        return
    if login_type(found[host]) not in SUPPORTED_LOGINS:
        print("\nNot trying to log in: python-kasa can't use this login type.")
        _save({"discovery": found[host]}, "probe")
        return

    print(f"\nLogging in to {host} ...")
    try:
        dev = await connect(host)
    except AuthenticationError as err:
        print(f"LOGIN FAILED: {err}\n-> The strip has a login we don't know (someone set it up"
              " with an account?). A factory reset clears it.")
        _save({"discovery": found[host], "login_error": str(err)}, "probe")
        return
    print(f"Logged in using: {', '.join(CRED_LOG.notes) or 'unknown'}")

    info = dev._info
    print(f"  Firmware: {info.get('fw_ver')}   Hardware: {info.get('hw_ver')}   Model: {info.get('model')}")
    print(f"  Wi-Fi:    {'connected to home network' if info.get('ssid') else 'setup mode'}"
          f"   signal: {info.get('rssi')} dBm")
    t = dev.modules.get("Time")
    if t:
        print(f"  Clock:    {t.time}   (PC: {datetime.now(ZoneInfo(LOCAL_TZ)):%Y-%m-%d %H:%M:%S})")

    dump: dict = {"discovery": found[host], "device_info": info, "login_used": CRED_LOG.notes}
    for m in ("component_nego", "get_auto_update_info", "get_connect_cloud_state",
              "get_matter_setup_info", "get_latest_fw"):
        dump[m] = await raw(dev, m)
    au = dump["get_auto_update_info"]
    print(f"  Auto firmware update: {au}")
    print(f"  Cloud connection:     {dump['get_connect_cloud_state']}")

    print("\n  Outlet | on  | watts | after power cut | timers on strip")
    dump["outlets"] = []
    for c in outlets(dev):
        ci = c._info
        cd = await raw(c, "get_countdown_rules", {"start_index": 0})
        sc = await raw(c, "get_schedule_rules", {"start_index": 0, "schedule_mode": ""})
        dump["outlets"].append({"info": ci, "countdown": cd, "schedule": sc,
                                "watts": watts(c)})
        timers = "countdown OK" if "error" not in cd else "countdown ERR"
        timers += ", schedule OK" if "error" not in sc else ", schedule ERR"
        print(f"  {ci.get('position'):>6} | {'on ' if c.is_on else 'off'} | {watts(c)!s:>5} |"
              f" {ci.get('default_states', {}).get('type', '?'):<15} | {timers}")
    path = _save(dump, "probe")
    print(f"\nFull dump: {path}")
    await dev.disconnect()


async def cmd_join(args) -> None:
    """WRITES: put a factory-fresh strip (on its setup network) onto your Wi-Fi."""
    host = args.host or wifi_gateway()
    print(f"Logging in to the strip at {host} (its setup network) ...")
    creds = make_login()
    CRED_LOG.notes.clear()
    dev = await Discover.discover_single(host, credentials=creds)
    if dev is None:
        sys.exit(f"Nothing answered at {host}. Is the PC connected to the strip's setup network?")
    await dev.update()
    print(f"Logged in using: {', '.join(CRED_LOG.notes) or 'unknown'}")

    nets = await dev.wifi_scan()
    match = [n for n in nets if n.ssid == args.ssid]
    if not match:
        print("The strip can't see that network. Networks it can see (2.4 GHz only):")
        for n in sorted(nets, key=lambda n: -n.signal_level):
            print(f"  {n.ssid!r}  signal {n.signal_level}  {n.key_type}")
        sys.exit(1)
    keytype = match[0].key_type
    password = getpass.getpass(f"Wi-Fi password for {args.ssid!r} (not shown, not saved): ")
    if input("This joins the strip to that network and sets its local login. Type JOIN: ") != "JOIN":
        sys.exit("Cancelled; nothing sent.")
    await dev.wifi_join(args.ssid, password, keytype=keytype)
    print("Sent. The strip leaves setup mode now (its setup network disappears).\n"
          "1) Reconnect this PC to your home Wi-Fi.\n"
          "2) Run: probe   (it should find the strip on your home network)")


async def cmd_test(args) -> None:
    """WRITES on an EMPTY strip only: outlet switching, on-strip timer, clock."""
    host = await pick_host(args.host, args.target)
    dev = await connect(host)
    outs = outlets(dev)
    loads = {c._info.get("position"): watts(c) for c in outs}
    if any(w is None for w in loads.values()):
        sys.exit(f"Can't read power on every outlet ({loads}); refusing to switch anything.")
    busy = {p: w for p, w in loads.items() if w > EMPTY_WATTS}
    if busy:
        sys.exit(f"Something is plugged in and drawing power: {busy}. Unplug everything first.")
    start_states = {c._info.get("position"): c.is_on for c in outs}
    print(f"Strip at {host} is empty ({loads}). Starting.")
    results: dict = {}

    # 1. Each outlet off then on.
    for c in outs:
        p = c._info.get("position")
        await c.turn_off(); await dev.update()
        off_ok = not c.is_on
        await c.turn_on(); await dev.update()
        on_ok = c.is_on
        results[f"outlet {p} off/on"] = off_ok and on_ok
        print(f"  outlet {p}: off {'OK' if off_ok else 'FAIL'}, on {'OK' if on_ok else 'FAIL'}")

    # 2. "Back on in N seconds" timer stored on the strip, PC disconnected meanwhile.
    n = args.delay
    c = next(c for c in outs if c._info.get("position") == args.outlet)
    await c.turn_off()
    before = await raw(c, "get_countdown_rules", {"start_index": 0})
    rule = {"delay": n, "desired_states": {"on": True}, "enable": True, "remain": n}
    if isinstance(before, dict) and before.get("rule_list"):
        add = await raw(c, "edit_countdown_rule", {"id": before["rule_list"][0]["id"], **rule})
    else:
        add = await raw(c, "add_countdown_rule", rule)
    after = await raw(c, "get_countdown_rules", {"start_index": 0})
    print(f"  timer on outlet {args.outlet}: set -> {add}; strip reports {after}")
    await dev.disconnect()
    print(f"  PC disconnected. Waiting {n + 10} s for the strip to turn outlet {args.outlet} back on by itself ...")
    await asyncio.sleep(n + 10)
    dev = await connect(host)
    c = next(c for c in outlets(dev) if c._info.get("position") == args.outlet)
    results["timer turned outlet back on"] = c.is_on
    print(f"  outlet {args.outlet} is {'ON - timer works on the strip' if c.is_on else 'still OFF - timer FAILED'}")

    # 3. Clock.
    t = dev.modules.get("Time")
    if t:
        old = t.time
        await t.set_time(datetime.now(ZoneInfo(LOCAL_TZ)))
        await dev.update()
        results["clock set"] = abs((t.time - datetime.now(ZoneInfo(LOCAL_TZ))).total_seconds()) < 5
        print(f"  clock: was {old}, now {t.time}")

    # 4. Automatic firmware updates off: an update could switch the strip to a
    #    login python-kasa can't use (TPAP). Same call python-kasa's Firmware module makes.
    au = await raw(dev, "get_auto_update_info")
    if isinstance(au, dict) and "enable" in au:
        if au["enable"]:
            await raw(dev, "set_auto_update_info", {**au, "enable": False})
            au = await raw(dev, "get_auto_update_info")
        results["auto-update off"] = au.get("enable") is False
        print(f"  automatic firmware updates: {'OFF' if au.get('enable') is False else 'STILL ON'} ({au})")
    else:
        results["auto-update off"] = False
        print(f"  automatic firmware updates: couldn't read ({au})")

    # Put outlets back the way they were.
    for c in outlets(dev):
        if c.is_on != start_states.get(c._info.get("position"), True):
            await (c.turn_on() if start_states[c._info.get("position")] else c.turn_off())
    await dev.disconnect()
    path = _save({"host": host, "results": results}, "test")
    print(f"\nSummary: {results}\nSaved: {path}")


# --------------------------------------------------------------------------- main


def _save(obj: dict, kind: str) -> Path:
    BACKUPS.mkdir(exist_ok=True)
    path = BACKUPS / f"p316m_{kind}_{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
    return path


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("wifi", help="read-only: look for the strip's setup network")
    for name, help_ in (("probe", "read-only: find the strip and read everything"),
                        ("test", "WRITES, empty strip only: switching, on-strip timer, clock")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--host", help="strip IP (default: find it by broadcast)")
        p.add_argument("--target", default="255.255.255.255", help="broadcast address")
        if name == "test":
            p.add_argument("--outlet", type=int, default=6, help="outlet for the timer test")
            p.add_argument("--delay", type=int, default=30, help="timer seconds")
    j = sub.add_parser("join", help="WRITES: put the strip on your Wi-Fi from its setup network")
    j.add_argument("--ssid", required=True, help="your home Wi-Fi name (2.4 GHz)")
    j.add_argument("--host", help="strip IP on its setup network (default: the Wi-Fi gateway)")
    args = ap.parse_args()

    if args.cmd == "wifi":
        cmd_wifi(args)
    else:
        asyncio.run({"probe": cmd_probe, "join": cmd_join, "test": cmd_test}[args.cmd](args))


if __name__ == "__main__":
    main()
