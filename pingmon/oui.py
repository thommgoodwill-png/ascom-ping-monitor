"""MAC -> vendor lookup from a bundled OUI prefix table.

Ships a curated list of common network-gear vendors (switches, APs, phones,
servers, virtualisation) so it works fully offline. Users can drop a fuller
IEEE OUI export at <data dir>/oui.csv (lines: HEXPREFIX,Vendor) to extend it.
"""
import os
import threading

from . import database

_map = None
_lock = threading.Lock()


def _load():
    global _map
    m = {}
    # bundled table
    bundled = os.path.join(os.path.dirname(__file__), "data", "oui.csv")
    for path in (bundled, os.path.join(database.DATA_DIR, "oui.csv")):
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "," not in line:
                        continue
                    prefix, vendor = line.split(",", 1)
                    prefix = prefix.replace(":", "").replace("-", "").replace(" ", "").upper()
                    if len(prefix) >= 6:
                        m[prefix[:6]] = vendor.strip()
        except OSError:
            continue
    _map = m


def vendor(mac):
    """Return the vendor for a MAC, or None."""
    if not mac:
        return None
    global _map
    if _map is None:
        with _lock:
            if _map is None:
                _load()
    key = mac.replace(":", "").replace("-", "").upper()[:6]
    return _map.get(key)
