"""pythonw -m app.tray: run Reef Control in the background, with an icon in the
notification area (click it to open the dashboard, right-click to restart or
quit).

Started at sign-in by the "Reef Control" scheduled task (install-startup.ps1).
There's no console window, so the output goes to data\\reef-control.log.
"""
from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import DATA_DIR, WEB_DIR, load

LOG_PATH = DATA_DIR / "reef-control.log"
ICON_PATH = WEB_DIR / "reef.ico"
PROJECT_DIR = Path(__file__).resolve().parent.parent
RESTART_FLAG = "--restart"
RESTART_WAIT = 60  # seconds the new copy waits for the old one to let go of the port
log = logging.getLogger("reef.tray")


def already_running(port: int) -> bool:
    """True if something (normally another Reef Control) is serving on this PC's port."""
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def wait_until_free(port: int, timeout: float = RESTART_WAIT) -> bool:
    """Waits for the port to be free (the copy being restarted has stopped)."""
    end = time.monotonic() + timeout
    while already_running(port):
        if time.monotonic() > end:
            return False
        time.sleep(0.5)
    return True


def restart_command() -> list[str]:
    """This app again, in the background (pythonw: no console window)."""
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    return [str(pythonw if pythonw.exists() else exe), "-m", "app.tray", RESTART_FLAG]


def spawn(cmd: list[str]) -> None:
    """Starts cmd as a process of its own that outlives this one: detached,
    and outside this process's job if it's in one (Task Scheduler)."""
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    for extra in (subprocess.CREATE_BREAKAWAY_FROM_JOB, 0):
        try:
            subprocess.Popen(cmd, cwd=PROJECT_DIR, creationflags=flags | extra, close_fds=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except OSError as e:
            if not extra:
                raise
            log.info("couldn't start outside this process's job (%s); starting it normally", e)


class _LogWriter:
    """Stands in for stdout/stderr, which pythonw doesn't have."""

    def __init__(self, logger: logging.Logger, level: int):
        self.logger, self.level, self.buf = logger, level, ""

    def write(self, text: str) -> int:
        self.buf += text
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            if line.strip():
                self.logger.log(self.level, line.rstrip())
        return len(text)

    def flush(self):
        pass

    def isatty(self) -> bool:
        return False


def setup_logging():
    DATA_DIR.mkdir(exist_ok=True)
    handler = RotatingFileHandler(LOG_PATH, maxBytes=2_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)  # app.main's basicConfig() then leaves logging as it is
    root.setLevel(logging.INFO)
    logging.raiseExceptions = False  # a failing log write must never loop back through stderr
    if sys.stderr is None:  # pythonw
        sys.stdout = _LogWriter(logging.getLogger("stdout"), logging.INFO)
        sys.stderr = _LogWriter(logging.getLogger("stderr"), logging.WARNING)
    else:  # started from a console: show the log there too
        root.addHandler(logging.StreamHandler())


def run_tray(server, url: str, restarted: bool = False) -> bool:
    """Runs the uvicorn server on this (the main) thread with the icon on its own thread,
    and returns once the user quits or restarts: True means start again."""
    import pystray
    from PIL import Image

    image = Image.open(ICON_PATH)
    stopped_image = image.convert("LA").convert("RGBA")
    quitting, restarting, stopped = threading.Event(), threading.Event(), threading.Event()

    def open_page():
        webbrowser.open(url)

    def open_log():
        os.startfile(LOG_PATH)

    def stop(icon, title: str):
        quitting.set()
        if stopped.is_set():
            icon.stop()
        else:
            icon.title = title
            server.should_exit = True  # uvicorn shuts down as it would on Ctrl+C

    def quit_(icon):
        stop(icon, "Reef Control: stopping…")

    def restart(icon):
        log.info("restart requested from the icon")
        restarting.set()
        stop(icon, "Reef Control: restarting…")

    def setup(icon):
        icon.visible = True
        while not server.started and not stopped.is_set():
            time.sleep(0.5)
        if server.started and not stopped.is_set():
            icon.title = f"Reef Control: {url}"
            if restarted:
                try:
                    icon.notify("Reef Control restarted.", "Reef Control")
                except Exception:
                    pass

    icon = pystray.Icon(
        "reef_control", image, "Reef Control: starting…",
        pystray.Menu(
            pystray.MenuItem("Open Reef Control", open_page, default=True),
            pystray.MenuItem("Open log", open_log),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Restart Reef Control", restart),
            pystray.MenuItem("Quit Reef Control", quit_),
        ),
    )
    tray = threading.Thread(target=icon.run, kwargs={"setup": setup}, name="tray", daemon=True)
    tray.start()

    crashed = False
    try:
        server.run()
    except SystemExit:  # uvicorn exits this way when it can't start, e.g. the port is taken
        crashed = True
    except Exception:
        log.exception("Reef Control crashed")
        crashed = True
    finally:
        stopped.set()

    if quitting.is_set() or (server.started and not crashed):
        icon.stop()
    else:
        # Keep a grey icon so a failed start doesn't go unnoticed.
        log.error("Reef Control stopped by itself; quit it from the icon after reading the log")
        icon.icon = stopped_image
        icon.title = "Reef Control stopped (see the log)"
        try:
            icon.notify("Reef Control stopped. Right-click its icon to open the log.", "Reef Control")
        except Exception:
            pass
    tray.join()
    return restarting.is_set()


def main():
    setup_logging()
    cfg = load()
    url = f"http://localhost:{cfg.port}"
    restarted = RESTART_FLAG in sys.argv[1:]
    if restarted and not wait_until_free(cfg.port):
        log.warning("the previous copy still holds port %s after %s s", cfg.port, RESTART_WAIT)
    if already_running(cfg.port):
        log.info("Reef Control is already running; opening %s", url)
        webbrowser.open(url)
        return

    import uvicorn

    log.info("starting Reef Control at %s%s", url, " (restart)" if restarted else "")
    server = uvicorn.Server(uvicorn.Config(
        "app.main:app", host="127.0.0.1", port=cfg.port,
        log_config=None,    # use the log file set up above
        access_log=False,   # one line per page request would bury everything else
    ))
    if run_tray(server, url, restarted):
        # A fresh process, so it picks up any changed code. It waits for this
        # one to let go of the port before starting.
        log.info("Reef Control stopped; starting it again")
        spawn(restart_command())
    else:
        log.info("Reef Control stopped")


if __name__ == "__main__":
    main()
