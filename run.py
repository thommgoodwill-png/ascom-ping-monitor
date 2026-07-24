#!/usr/bin/env python3
"""Ascom Network Monitor entry point (production server via waitress)."""
import logging
import os
import sys
import threading
import webbrowser

_FROZEN = getattr(sys, "frozen", False)   # running as a PyInstaller exe

handlers = [logging.StreamHandler()]
if _FROZEN:
    # windowed exe has no console — keep a log file next to the data
    from pingmon import database
    os.makedirs(database.DATA_DIR, exist_ok=True)
    handlers.append(logging.FileHandler(
        os.path.join(database.DATA_DIR, "pingmon.log"), encoding="utf-8"))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                    handlers=handlers)

from pingmon.app import create_app  # noqa: E402

app = create_app()


def _tray_icon(port):
    """Optional Windows system-tray icon with Open / Quit. Needs pystray+Pillow;
    silently skipped if unavailable (the GUI Quit button still works)."""
    try:
        import pystray
        from PIL import Image, ImageDraw
    except Exception:
        return
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([2, 2, 62, 62], radius=12, fill=(218, 41, 28, 255))
    try:
        from PIL import ImageFont
        f = ImageFont.truetype("arialbd.ttf", 46)
    except Exception:
        f = None
    d.text((18, 4), "a", fill=(255, 255, 255, 255), font=f)

    def _open(icon, item):
        webbrowser.open(f"http://127.0.0.1:{port}")

    def _quit(icon, item):
        icon.stop()
        os._exit(0)

    menu = pystray.Menu(
        pystray.MenuItem("Open Network Monitor", _open, default=True),
        pystray.MenuItem("Quit", _quit))
    icon = pystray.Icon("AscomNetworkMonitor", img, "Ascom Network Monitor", menu)
    threading.Thread(target=icon.run, daemon=True, name="tray").start()


if __name__ == "__main__":
    host = os.environ.get("PINGMON_HOST", "0.0.0.0")
    port = int(os.environ.get("PINGMON_PORT", "8080"))
    if _FROZEN:
        _tray_icon(port)
        if os.environ.get("PINGMON_NO_BROWSER") != "1":
            threading.Timer(1.5, lambda: webbrowser.open(
                f"http://127.0.0.1:{port}")).start()
    try:
        from waitress import serve
        logging.getLogger("pingmon").info("listening on http://%s:%s", host, port)
        serve(app, host=host, port=port, threads=8)
    except ImportError:
        logging.getLogger("pingmon").warning(
            "waitress not installed - falling back to Flask dev server")
        app.run(host=host, port=port)
