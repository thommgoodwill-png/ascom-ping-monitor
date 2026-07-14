#!/usr/bin/env python3
"""Ascom Ping Monitor entry point (production server via waitress)."""
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

if __name__ == "__main__":
    host = os.environ.get("PINGMON_HOST", "0.0.0.0")
    port = int(os.environ.get("PINGMON_PORT", "8080"))
    if _FROZEN and os.environ.get("PINGMON_NO_BROWSER") != "1":
        def _open():
            try:
                webbrowser.open(f"http://127.0.0.1:{port}")
            except Exception:
                pass
        threading.Timer(1.5, _open).start()
    try:
        from waitress import serve
        logging.getLogger("pingmon").info("listening on http://%s:%s", host, port)
        serve(app, host=host, port=port, threads=8)
    except ImportError:
        logging.getLogger("pingmon").warning(
            "waitress not installed - falling back to Flask dev server")
        app.run(host=host, port=port)
