"""python -m app: start the dashboard on this PC only and open it."""
import threading
import webbrowser

import uvicorn

from .config import load
from .tray import already_running

cfg = load()
url = f"http://localhost:{cfg.port}"
if already_running(cfg.port):
    # Most likely the copy in the notification area; a second one would fight it for the lights.
    print(f"Reef Control is already running; opening {url}")
    webbrowser.open(url)
    raise SystemExit
threading.Timer(1.5, lambda: webbrowser.open(url)).start()
print(f"Reef Control running at {url} (close this window to stop it)")
uvicorn.run("app.main:app", host="127.0.0.1", port=cfg.port, log_level="info")
