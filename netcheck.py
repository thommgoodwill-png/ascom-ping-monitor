"""ASCII call feed over IP.

Streams one delimited line per nurse-call transition to a third-party receiver
(a display board, paging gateway, logger, …). Each line is::

    dutyarea|position|location|callstate<EOL>

* **dutyarea** — the duty-area name the call belongs to (e.g. ``Test Rig``)
* **position** — the sub-location within the room: ``Bed`` or ``Bathroom``
* **location** — the room / point name (e.g. ``Bedroom 1``)
* **callstate** — the call text on raise (``Patient Call``, ``Emergency`` …) or
  the reset word on clear (``Reset`` by default)

``<EOL>`` defaults to a carriage return (``\\r``). The transport is selectable:

* **tcp_client** — the monitor connects out to ``host:port`` and streams; it
  reconnects automatically and replays the current active calls on connect.
* **tcp_server** — the monitor listens on ``port``; receivers connect in and are
  each sent the active-call snapshot, then the live stream.
* **udp** — each line is sent as a datagram to ``host:port`` (fire-and-forget).

The feed is purely OUTBOUND: a ``Reset`` line only reflects that the nurse-call
system cleared the call. Nothing here writes back to the life-safety system.
"""
import socket
import threading
import time

from . import settings

_EOL = {"cr": "\r", "crlf": "\r\n", "lf": "\n"}


def load_cfg():
    g = settings.get
    return {
        "enabled": bool(g("feed_enabled")),
        "mode": (g("feed_mode") or "tcp_client").strip(),
        "host": (g("feed_host") or "").strip(),
        "port": int(g("feed_port") or 0),
        "eol": _EOL.get((g("feed_eol") or "cr").strip(), "\r"),
        "clear_text": (g("feed_clear_text") or "Reset").strip() or "Reset",
        "heartbeat_on": bool(g("feed_heartbeat_enabled")),
        "heartbeat_secs": int(g("feed_heartbeat_secs") or 0),
        "heartbeat_text": (g("feed_heartbeat_text") or "HEARTBEAT").strip("\r\n")
                          or "HEARTBEAT",
    }


def _clean(s):
    """Keep the delimited line intact — no pipes or line breaks inside a field."""
    return (str(s if s is not None else "")
            .replace("|", " ").replace("\r", " ").replace("\n", " ").strip())


def format_line(dutyarea, position, location, callstate, eol="\r"):
    return "|".join(_clean(x) for x in
                    (dutyarea, position, location, callstate)) + eol


class CallFeed:
    """Background sender. ``emit()`` from any thread; delivery happens on the
    feed's own thread so a slow/broken receiver never blocks the poller."""

    def __init__(self):
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="callfeed")
        self._lock = threading.Lock()
        self._queue = []                 # outgoing lines awaiting send
        self._active = {}                # key -> line (current live calls, for snapshot)
        self._clients = []               # connected sockets (tcp_server)
        self._sock = None                # client/udp socket
        self._srv = None                 # listening socket (tcp_server)
        self._cur = None                 # (mode, host, port) currently bound
        self.connected = False
        self.last_error = None
        self.last_sent = None
        self.sent_count = 0
        self._last_hb = 0            # when the last heartbeat line was queued

    # ---- lifecycle ----

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def status(self):
        c = load_cfg()
        with self._lock:
            active = len(self._active)
            clients = len(self._clients)
        return {
            "enabled": c["enabled"], "mode": c["mode"],
            "host": c["host"], "port": c["port"],
            "connected": self.connected, "last_error": self.last_error,
            "last_sent": self.last_sent, "sent_count": self.sent_count,
            "active_calls": active, "clients": clients,
        }

    # ---- public emit API (called from the bridge) ----

    def emit(self, key, dutyarea, position, location, callstate, active):
        """Queue one line. ``active=True`` marks a live call (kept for the
        connect snapshot); ``active=False`` is a clear/reset and drops it."""
        cfg = load_cfg()
        if not cfg["enabled"]:
            return
        line = format_line(dutyarea, position, location, callstate, cfg["eol"])
        with self._lock:
            self._queue.append(line)
            if key is not None:
                if active:
                    self._active[key] = line
                else:
                    self._active.pop(key, None)
        self._wake.set()

    def emit_call(self, key, dutyarea, position, location, callstate):
        """A call was raised — callstate is the friendly call text."""
        self.emit(key, dutyarea, position, location, callstate, active=True)

    def emit_reset(self, key, dutyarea, position, location):
        """A call was cleared/reset — sends the configured reset word."""
        self.emit(key, dutyarea, position, location, load_cfg()["clear_text"],
                  active=False)

    def send_test(self):
        """Push a sample line immediately; raises if the feed isn't sendable."""
        cfg = load_cfg()
        if not cfg["enabled"]:
            raise RuntimeError("enable the call feed first")
        if cfg["mode"] in ("tcp_client", "udp") and (not cfg["host"] or not cfg["port"]):
            raise RuntimeError("set the destination host and port")
        if cfg["mode"] == "tcp_server" and not cfg["port"]:
            raise RuntimeError("set the listen port")
        line = format_line("Test Rig", "Bed", "Bedroom 1", "Patient Call", cfg["eol"])
        with self._lock:
            self._queue.append(line)
        self._wake.set()
        return {"mode": cfg["mode"], "sample": line.rstrip("\r\n")}

    # ---- transport thread ----

    def _run(self):
        while not self._stop.is_set():
            cfg = load_cfg()
            if not cfg["enabled"]:
                self._teardown()
                self.connected = False
                self._wait(2)
                continue
            try:
                self._ensure_transport(cfg)
                self._maybe_heartbeat(cfg)
                self._drain(cfg)
            except Exception as e:                     # keep the thread alive
                self.last_error = f"{type(e).__name__}: {e}"
                self.connected = False
                self._teardown()
                self._wait(3)
                continue
            self._wait(1)

    def _wait(self, secs):
        self._wake.wait(secs)
        self._wake.clear()

    def _maybe_heartbeat(self, cfg):
        """Queue a keepalive line every ``heartbeat_secs`` (0 = off). Sent through
        the same transport as calls — for a TCP client a failed heartbeat send is
        what surfaces a silently-dropped link and triggers a reconnect."""
        hb = cfg["heartbeat_secs"]
        if not cfg["heartbeat_on"] or hb <= 0:
            return
        now = time.time()
        if now - self._last_hb < hb:
            return
        self._last_hb = now
        line = cfg["heartbeat_text"] + cfg["eol"]
        with self._lock:
            self._queue.append(line)

    def _target_key(self, cfg):
        return (cfg["mode"], cfg["host"], cfg["port"])

    def _ensure_transport(self, cfg):
        if self._cur == self._target_key(cfg) and (
                self._sock or self._srv or cfg["mode"] == "tcp_server"):
            return
        self._teardown()
        mode = cfg["mode"]
        if mode == "udp":
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.connected = True
            self.last_error = None
        elif mode == "tcp_server":
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("0.0.0.0", cfg["port"]))
            srv.listen(5)
            srv.settimeout(0.5)
            self._srv = srv
            threading.Thread(target=self._accept_loop, args=(srv, cfg),
                             daemon=True, name="callfeed-accept").start()
            self.connected = True
            self.last_error = None
        else:                                          # tcp_client
            s = socket.create_connection((cfg["host"], cfg["port"]), timeout=5)
            s.settimeout(5)
            self._sock = s
            self.connected = True
            self.last_error = None
            self._send_snapshot_client(cfg)
        self._cur = self._target_key(cfg)

    def _accept_loop(self, srv, cfg):
        while not self._stop.is_set() and self._srv is srv:
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            conn.settimeout(5)
            with self._lock:
                snap = list(self._active.values())
                self._clients.append(conn)
            try:
                for line in snap:
                    conn.sendall(line.encode("ascii", "replace"))
            except OSError:
                self._drop_client(conn)

    def _send_snapshot_client(self, cfg):
        with self._lock:
            snap = list(self._active.values())
        for line in snap:
            self._sock.sendall(line.encode("ascii", "replace"))

    def _drain(self, cfg):
        while True:
            with self._lock:
                if not self._queue:
                    return
                line = self._queue.pop(0)
            data = line.encode("ascii", "replace")
            mode = cfg["mode"]
            if mode == "udp":
                self._sock.sendto(data, (cfg["host"], cfg["port"]))
            elif mode == "tcp_server":
                with self._lock:
                    clients = list(self._clients)
                for c in clients:
                    try:
                        c.sendall(data)
                    except OSError:
                        self._drop_client(c)
            else:                                      # tcp_client
                self._sock.sendall(data)
            self.sent_count += 1
            self.last_sent = time.time()

    def _drop_client(self, conn):
        with self._lock:
            if conn in self._clients:
                self._clients.remove(conn)
        try:
            conn.close()
        except OSError:
            pass

    def _teardown(self):
        for attr in ("_sock", "_srv"):
            s = getattr(self, attr)
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
                setattr(self, attr, None)
        with self._lock:
            clients, self._clients = self._clients, []
        for c in clients:
            try:
                c.close()
            except OSError:
                pass
        self._cur = None
