"""Built-in file servers: HTTP, HTTPS, FTP and TFTP over one shared folder.

Why this exists: commissioning Ascom hardware means handing files to devices
that only speak old protocols. Handsets and IP-DECT base stations pull firmware
and configuration over TFTP or FTP; newer kit uses HTTP(S). On site that
normally means installing a separate server, or borrowing someone's laptop.
This puts all four behind one switch, serving one folder, on the machine that
is already sitting on the customer's network.

Everything here is standard library. FTP and TFTP are implemented directly
rather than pulled in as dependencies, because this ships as a single
PyInstaller .exe and a server that quietly fails to be bundled is worse than
no server at all.

Ground rules, in order of importance:

  * Every protocol resolves paths through _safe_path(), which refuses anything
    that escapes the served folder. A file server that can be talked into
    handing over C:\\Windows\\... is not a feature.
  * Every server is off by default and writes are off by default. Enabling a
    server is a deliberate act; so is letting devices write to it.
  * Nothing here touches the monitoring side. A file transfer must never be
    able to stall pinging, so every server runs on its own daemon threads.
"""
import base64
import errno
import hmac
import logging
import os
import re
import shutil
import socket
import socketserver
import ssl
import struct
import threading
import time
from collections import deque
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from . import database, settings

log = logging.getLogger("pingmon.fileserv")

# Protocol identifiers used as dict keys, in settings names (fs_<name>_*) and
# in the GUI. Order is the order the cards appear on the page.
PROTOCOLS = ("http", "https", "ftp", "tftp")

DEFAULT_ROOT = os.path.join(database.DATA_DIR, "fileserver")

# A device that asks for a file bigger than this is almost certainly a bug or
# an attack, and a runaway upload would fill the monitor's disk.
_ABS_MAX_UPLOAD = 10 * 1024 * 1024 * 1024      # 10 GB ceiling on the setting


# --------------------------------------------------------------------------
# shared: the served folder, path safety, transfer log
# --------------------------------------------------------------------------

def root_dir(create=True):
    """The folder every protocol serves. Blank setting = <data dir>/fileserver."""
    root = (settings.get("fs_root") or "").strip() or DEFAULT_ROOT
    if create:
        try:
            os.makedirs(root, exist_ok=True)
        except OSError as e:
            log.warning("cannot create file server root %s: %s", root, e)
    return root


def _safe_path(rel, root=None):
    """Resolve a client-supplied path inside the served folder, or raise.

    This is the single choke point for every protocol. It normalises the path,
    resolves symlinks, and then insists the result is still under the root —
    so "../../etc/passwd", "..\\..\\windows\\win.ini", an absolute path and a
    symlink planted in the folder all fail here rather than somewhere deeper.
    """
    root = os.path.realpath(root or root_dir())
    rel = (rel or "").replace("\\", "/").lstrip("/")
    # drop drive letters and UNC prefixes a Windows client might send
    rel = re.sub(r"^[A-Za-z]:", "", rel).lstrip("/")
    full = os.path.realpath(os.path.join(root, rel))
    if full != root and not full.startswith(root + os.sep):
        raise ValueError("path outside the served folder")
    return full


def _discard_partial(path, keep=0):
    """Undo a failed upload.

    A half-written file left in the folder is worse than no file: the next
    device along downloads a truncated firmware image and bricks itself. On a
    failed overwrite we delete it; on a failed append we cut it back to the
    length it had before the transfer started.
    """
    try:
        if keep > 0:
            with open(path, "r+b") as f:
                f.truncate(keep)
        elif os.path.exists(path):
            os.remove(path)
    except OSError as e:
        log.warning("could not clean up partial upload %s: %s", path, e)


def max_upload_bytes():
    mb = int(settings.get("fs_max_upload_mb") or 0)
    return min(mb * 1024 * 1024, _ABS_MAX_UPLOAD) if mb > 0 else _ABS_MAX_UPLOAD


# Recent transfers, newest last. Purely for the GUI — this is the thing that
# turns "the handset isn't upgrading" into "the handset never asked" or "it
# asked for a filename that isn't there".
_XFERS = deque(maxlen=300)
_xfer_lock = threading.Lock()


def record(proto, peer, op, path, nbytes=0, ok=True, error=""):
    with _xfer_lock:
        _XFERS.append({"ts": time.time(), "proto": proto, "peer": peer,
                       "op": op, "path": path, "bytes": int(nbytes),
                       "ok": bool(ok), "error": str(error)[:200]})
    if ok:
        log.info("%s %s %s (%d bytes) for %s", proto, op, path, nbytes, peer)
    else:
        log.info("%s %s %s FAILED for %s: %s", proto, op, path, peer, error)


def transfers(limit=100):
    with _xfer_lock:
        return list(_XFERS)[-limit:][::-1]


def clear_transfers():
    with _xfer_lock:
        _XFERS.clear()


# --------------------------------------------------------------------------
# base server: common lifecycle so the manager can treat all four alike
# --------------------------------------------------------------------------

class _Server:
    proto = "?"

    def __init__(self, cfg):
        self.cfg = cfg                   # snapshot of the settings it started with
        self.error = ""
        self.started = 0.0
        self._running = False

    # -- subclasses implement these two --
    def _start(self):
        raise NotImplementedError

    def _stop(self):
        pass

    def start(self):
        try:
            self._start()
        except OSError as e:
            self.error = _bind_error(e, self.cfg.get("port"))
            log.warning("%s server did not start: %s", self.proto, self.error)
            return False
        except Exception as e:                       # noqa: BLE001 - report anything
            self.error = f"{type(e).__name__}: {e}"
            log.warning("%s server did not start: %s", self.proto, self.error)
            return False
        self._running = True
        self.started = time.time()
        self.error = ""
        log.info("%s server listening on %s:%s (root %s, uploads %s)",
                 self.proto, self.cfg.get("bind"), self.cfg.get("port"),
                 root_dir(create=False), "on" if self.cfg.get("upload") else "off")
        return True

    def stop(self):
        try:
            self._stop()
        except Exception as e:                       # noqa: BLE001
            log.debug("%s server stop: %s", self.proto, e)
        self._running = False

    @property
    def running(self):
        return self._running


def _bind_error(e, port):
    """Turn the OS error into something an engineer on site can act on."""
    if e.errno in (errno.EACCES, errno.EPERM):
        return (f"permission denied binding port {port} — ports below 1024 need "
                f"root on Linux; on Windows run the app as Administrator, or "
                f"pick a port above 1024")
    if e.errno in (errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", -1)):
        return (f"port {port} is already in use — another server (or a previous "
                f"copy of this one) has it open")
    if e.errno in (errno.EADDRNOTAVAIL,):
        return "the bind address does not exist on this machine"
    return f"{type(e).__name__}: {e}"


# --------------------------------------------------------------------------
# HTTP / HTTPS
# --------------------------------------------------------------------------

class _HttpHandler(SimpleHTTPRequestHandler):
    """Directory listing + download, and optionally PUT/POST upload.

    server_version is deliberately vague: there is no reason to advertise the
    Python version to everything on the customer's LAN.
    """
    server_version = "AscomFileServer"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # set per-instance by the factory below
    fs_cfg = {}

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=self.fs_cfg.get("root") or root_dir(), **kw)

    # -- plumbing ----------------------------------------------------------
    def log_message(self, fmt, *args):      # keep stderr clean; we log transfers
        log.debug("%s %s", self.address_string(), fmt % args)

    def _peer(self):
        return self.client_address[0]

    def _authorised(self):
        user = (self.fs_cfg.get("user") or "").strip()
        pwd = self.fs_cfg.get("password") or ""
        if not user:
            return True                      # no credentials configured = open
        got = self.headers.get("Authorization", "")
        if got.startswith("Basic "):
            try:
                raw = base64.b64decode(got[6:]).decode("utf-8", "replace")
            except Exception:                # noqa: BLE001
                raw = ""
            u, _, p = raw.partition(":")
            # compare_digest on both halves so a wrong username costs the same
            # time as a wrong password
            if hmac.compare_digest(u, user) and hmac.compare_digest(p, pwd):
                return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Ascom file server"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _refuse(self, code, msg, pending=0):
        """Send a short plain-text response, optionally after draining a body.

        Refusing an upload without reading what the client is still sending
        gives it a broken pipe instead of our status line, so it reports
        "connection reset" and the engineer has no idea the file was simply too
        big. Read a bounded amount first (a "lingering close" — the same thing
        nginx does), then say so and hang up rather than swallowing gigabytes.
        """
        drained = 0
        while drained < min(pending, 262144):
            chunk = self.rfile.read(min(65536, pending - drained))
            if not chunk:
                break
            drained += len(chunk)
        body = (msg + "\n").encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if drained < pending:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(body)

    # -- download ----------------------------------------------------------
    def do_GET(self):
        if not self._authorised():
            return
        path = self.translate_path(self.path)
        super().do_GET()
        if os.path.isfile(path):
            record(self.fs_cfg.get("proto", "http"), self._peer(), "GET",
                   _rel(path), os.path.getsize(path))

    def do_HEAD(self):
        if not self._authorised():
            return
        super().do_HEAD()

    # -- upload ------------------------------------------------------------
    def do_PUT(self):
        self._upload("PUT")

    def do_POST(self):
        self._upload("POST")

    def _upload(self, verb):
        proto = self.fs_cfg.get("proto", "http")
        if not self._authorised():
            return
        if not self.fs_cfg.get("upload"):
            try:
                pending = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                pending = 0
            record(proto, self._peer(), verb, self.path, 0, False, "uploads disabled")
            return self._refuse(403, "uploads are disabled for this server", pending)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return self._refuse(411, "Content-Length required")
        cap = max_upload_bytes()
        if length > cap:
            record(proto, self._peer(), verb, self.path, length, False, "too large")
            return self._refuse(413, f"file exceeds the {cap // (1024*1024)} MB limit",
                                length)
        try:
            dest = _safe_path(self.path.split("?", 1)[0])
        except ValueError:
            record(proto, self._peer(), verb, self.path, 0, False, "path refused")
            return self._refuse(403, "path outside the served folder", length)
        if dest.endswith(os.sep) or os.path.isdir(dest):
            return self._refuse(409, "that is a folder", length)
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            written = 0
            with open(dest, "wb") as f:
                while written < length:
                    chunk = self.rfile.read(min(65536, length - written))
                    if not chunk:
                        break
                    f.write(chunk)
                    written += len(chunk)
        except OSError as e:
            record(proto, self._peer(), verb, self.path, 0, False, str(e))
            return self._refuse(500, f"write failed: {e}")
        record(proto, self._peer(), verb, _rel(dest), written)
        self._refuse(201, "stored")


def _rel(path):
    """Path as the client sees it — relative to the served folder."""
    try:
        return "/" + os.path.relpath(path, os.path.realpath(root_dir(False))
                                     ).replace("\\", "/")
    except ValueError:
        return path


class HttpServer(_Server):
    proto = "http"

    def _start(self):
        cfg = dict(self.cfg)
        cfg["root"] = root_dir()
        cfg["proto"] = self.proto
        # a per-server handler class, so HTTP and HTTPS can hold different
        # settings at the same time without sharing class state
        handler = type("_Handler", (_HttpHandler,), {"fs_cfg": cfg})
        self._httpd = ThreadingHTTPServer((cfg["bind"], int(cfg["port"])), handler,
                                          bind_and_activate=False)
        self._httpd.daemon_threads = True
        self._httpd.allow_reuse_address = True
        try:
            self._httpd.server_bind()
            self._httpd.server_activate()
        except OSError:
            self._httpd.server_close()
            raise
        self._wrap(self._httpd)
        threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.4},
                         daemon=True, name=f"fs-{self.proto}").start()

    def _wrap(self, httpd):
        pass

    def _stop(self):
        if getattr(self, "_httpd", None):
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None


class HttpsServer(HttpServer):
    proto = "https"

    def _wrap(self, httpd):
        cert, key = _tls_files(self.cfg.get("cert"), self.cfg.get("key"))
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        self.cert_path = cert


def tls_available():
    """True when HTTPS can produce a certificate — an operator-supplied pair,
    one we generated earlier, or the 'cryptography' package to make one now.
    The page uses this to explain the problem before someone flips the switch
    and gets a bind error they can't interpret."""
    if (settings.get("fs_https_cert") or "").strip() and \
       (settings.get("fs_https_key") or "").strip():
        return True
    d = os.path.join(database.DATA_DIR, "certs")
    if os.path.exists(os.path.join(d, "fileserver.crt")) and \
       os.path.exists(os.path.join(d, "fileserver.key")):
        return True
    try:
        import cryptography           # noqa: F401
        return True
    except ImportError:
        return False


def _tls_files(cert, key):
    """Certificate + key for HTTPS: the operator's own if configured, otherwise
    a self-signed pair generated once and kept in the data directory.

    Devices on a commissioning LAN will not be validating this certificate
    anyway, and demanding a real one before HTTPS works at all would just mean
    HTTPS never gets used. The generated pair lives beside the database so it
    survives restarts and stays the same certificate across sessions.
    """
    cert = (cert or "").strip()
    key = (key or "").strip()
    if cert and key:
        if not os.path.exists(cert):
            raise FileNotFoundError(f"certificate file not found: {cert}")
        if not os.path.exists(key):
            raise FileNotFoundError(f"key file not found: {key}")
        return cert, key

    d = os.path.join(database.DATA_DIR, "certs")
    os.makedirs(d, exist_ok=True)
    cert, key = os.path.join(d, "fileserver.crt"), os.path.join(d, "fileserver.key")
    if os.path.exists(cert) and os.path.exists(key):
        return cert, key
    _generate_self_signed(cert, key)
    return cert, key


def _generate_self_signed(cert_path, key_path, days=3650):
    """Write a self-signed cert/key pair for this host."""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        import datetime
    except ImportError:
        raise RuntimeError(
            "HTTPS needs the 'cryptography' package to generate a certificate. "
            "Install it (pip install cryptography), or point the certificate and "
            "key settings at files you already have.")

    host = socket.gethostname() or "ascom-network-monitor"
    keyobj = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Ascom Network Monitor"),
        x509.NameAttribute(NameOID.COMMON_NAME, host),
    ])
    alt = [x509.DNSName(host), x509.DNSName("localhost")]
    for ip in _local_ips():
        try:
            import ipaddress
            alt.append(x509.IPAddress(ipaddress.ip_address(ip)))
        except ValueError:
            pass
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(keyobj.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName(alt), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None),
                           critical=True)
            .sign(keyobj, hashes.SHA256()))
    with open(key_path, "wb") as f:
        f.write(keyobj.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption()))
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    log.info("generated a self-signed certificate for %s (valid %d days)", host, days)


def _local_ips():
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            ips.add(info[4][0])
    except OSError:
        pass
    try:                                  # the address used to reach the network
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 53))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return {i for i in ips if not i.startswith("127.") and ":" not in i}


# --------------------------------------------------------------------------
# FTP  (RFC 959, plus the handful of extensions real clients expect)
# --------------------------------------------------------------------------

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


class _FtpSession(threading.Thread):
    """One control connection. Data connections are opened per transfer."""

    def __init__(self, conn, addr, cfg, server):
        super().__init__(daemon=True, name="fs-ftp-session")
        self.conn, self.addr, self.cfg, self.server = conn, addr, cfg, server
        self.rfile = conn.makefile("rb")
        self.user = ""
        self.authed = False
        self.cwd = "/"                     # virtual, always under the root
        self.binary = True
        self.rest = 0
        self.rename_from = None
        self.pasv_sock = None
        self.active_addr = None
        self.quit = False

    # -- control channel ---------------------------------------------------
    def reply(self, code, text):
        lines = text.split("\n")
        out = ""
        for i, line in enumerate(lines):
            sep = " " if i == len(lines) - 1 else "-"
            out += f"{code}{sep}{line}\r\n"
        try:
            self.conn.sendall(out.encode("utf-8", "replace"))
        except OSError:
            self.quit = True

    def run(self):
        try:
            self.conn.settimeout(300)
            self.reply(220, "Ascom Network Monitor file server")
            while not self.quit:
                raw = self.rfile.readline()
                if not raw:
                    break
                try:
                    line = raw.decode("utf-8", "replace").strip()
                except Exception:            # noqa: BLE001
                    continue
                if not line:
                    continue
                cmd, _, arg = line.partition(" ")
                cmd = cmd.upper().strip()
                arg = arg.strip()
                if cmd != "PASS":
                    log.debug("ftp %s: %s %s", self.addr[0], cmd, arg)
                self.dispatch(cmd, arg)
        except (OSError, socket.timeout):
            pass
        finally:
            self._close_pasv()
            try:
                self.rfile.close()
                self.conn.close()
            except OSError:
                pass
            self.server.forget(self)

    def dispatch(self, cmd, arg):
        # commands allowed before login
        if cmd == "USER":
            self.user = arg
            self.authed = False
            return self.reply(331, "Password required")
        if cmd == "PASS":
            return self._pass(arg)
        if cmd == "QUIT":
            self.reply(221, "Goodbye")
            self.quit = True
            return
        if cmd in ("FEAT", "SYST", "NOOP", "OPTS", "AUTH", "HELP"):
            return self._simple(cmd, arg)
        if not self.authed:
            return self.reply(530, "Log in first")

        handler = {
            "PWD": self._pwd, "XPWD": self._pwd,
            "CWD": self._cwd, "XCWD": self._cwd,
            "CDUP": lambda a: self._cwd(".."), "XCUP": lambda a: self._cwd(".."),
            "TYPE": self._type, "MODE": lambda a: self.reply(
                200, "Mode S") if a.upper() == "S" else self.reply(504, "Only mode S"),
            "STRU": lambda a: self.reply(
                200, "Structure F") if a.upper() == "F" else self.reply(504, "Only STRU F"),
            "PASV": self._pasv, "EPSV": self._epsv,
            "PORT": self._port, "EPRT": self._eprt,
            "LIST": lambda a: self._list(a, long=True),
            "NLST": lambda a: self._list(a, long=False),
            "MLSD": lambda a: self._list(a, long=True),
            "RETR": self._retr, "STOR": self._stor,
            "APPE": lambda a: self._stor(a, append=True),
            "STOU": self._stor,
            "REST": self._rest,
            "SIZE": self._size, "MDTM": self._mdtm,
            "DELE": self._dele, "MKD": self._mkd, "XMKD": self._mkd,
            "RMD": self._rmd, "XRMD": self._rmd,
            "RNFR": self._rnfr, "RNTO": self._rnto,
            "ABOR": lambda a: self.reply(226, "Nothing to abort"),
            "STAT": lambda a: self.reply(211, "Ascom file server"),
        }.get(cmd)
        if not handler:
            return self.reply(502, f"{cmd} not supported")
        try:
            handler(arg)
        except ValueError:
            self.reply(550, "Path outside the served folder")
        except OSError as e:
            self.reply(550, f"{e.strerror or e}")

    # -- auth --------------------------------------------------------------
    def _pass(self, given):
        want_user = (self.cfg.get("user") or "").strip()
        want_pass = self.cfg.get("password") or ""
        anon = bool(self.cfg.get("anonymous"))
        u = (self.user or "").strip()
        if anon and u.lower() in ("anonymous", "ftp"):
            self.authed = True
            return self.reply(230, "Anonymous access granted")
        if want_user and hmac.compare_digest(u, want_user) \
                and hmac.compare_digest(given, want_pass):
            self.authed = True
            return self.reply(230, "Logged in")
        if not want_user and anon:
            self.authed = True               # no account configured, anon allowed
            return self.reply(230, "Logged in")
        log.info("ftp %s: login refused for user %r", self.addr[0], u)
        self.reply(530, "Login incorrect")

    def _simple(self, cmd, arg):
        if cmd == "SYST":
            return self.reply(215, "UNIX Type: L8")
        if cmd == "NOOP":
            return self.reply(200, "OK")
        if cmd == "FEAT":
            return self.reply(211, "Features:\n UTF8\n SIZE\n MDTM\n REST STREAM\n"
                                   " PASV\n EPSV\n TVFS\nEnd")
        if cmd == "OPTS":
            if arg.upper().startswith("UTF8"):
                return self.reply(200, "UTF8 set to on")
            return self.reply(501, "Unknown option")
        if cmd == "AUTH":
            # plain FTP only — say so rather than leave a client hanging
            return self.reply(502, "TLS is not available on this server; use HTTPS "
                                   "for an encrypted transfer")
        return self.reply(214, "Ascom Network Monitor file server")

    # -- paths -------------------------------------------------------------
    def _virtual(self, arg):
        """Client path (absolute or relative to cwd) -> virtual path string."""
        arg = (arg or "").strip().strip('"')
        # some clients send "LIST -la" — the flags are not a path
        if arg.startswith("-"):
            arg = arg.partition(" ")[2].strip()
        if not arg:
            return self.cwd
        if arg.startswith("/"):
            v = arg
        else:
            v = self.cwd.rstrip("/") + "/" + arg
        parts = []
        for p in v.split("/"):
            if p in ("", "."):
                continue
            if p == "..":
                if parts:
                    parts.pop()
                continue
            parts.append(p)
        return "/" + "/".join(parts)

    def _real(self, arg):
        return _safe_path(self._virtual(arg))

    def _writable(self):
        if self.cfg.get("upload"):
            return True
        self.reply(550, "This server is read-only — enable uploads in Tools → "
                        "File servers to allow writing")
        return False

    def _pwd(self, arg):
        self.reply(257, f'"{self.cwd}" is the current directory')

    def _cwd(self, arg):
        v = self._virtual(arg)
        real = _safe_path(v)
        if not os.path.isdir(real):
            return self.reply(550, "No such directory")
        self.cwd = v
        self.reply(250, f'Directory changed to "{v}"')

    def _type(self, arg):
        t = (arg or "").upper()[:1]
        if t == "I" or t == "L":
            self.binary = True
        elif t == "A":
            self.binary = False
        else:
            return self.reply(504, "Unsupported type")
        self.reply(200, f"Type set to {t}")

    # -- data connections --------------------------------------------------
    def _close_pasv(self):
        if self.pasv_sock:
            try:
                self.pasv_sock.close()
            except OSError:
                pass
            self.pasv_sock = None

    def _open_passive(self):
        self._close_pasv()
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        lo, hi = self.cfg.get("pasv_from") or 0, self.cfg.get("pasv_to") or 0
        bind_ip = self.cfg.get("bind") or "0.0.0.0"
        if lo and hi and lo <= hi:
            # A fixed range is what makes this work through a firewall: the
            # engineer opens these ports once instead of the whole ephemeral
            # range. Fall back to any free port if they are all busy.
            for port in range(int(lo), int(hi) + 1):
                try:
                    s.bind((bind_ip, port))
                    break
                except OSError:
                    continue
            else:
                s.bind((bind_ip, 0))
        else:
            s.bind((bind_ip, 0))
        s.listen(1)
        s.settimeout(60)
        self.pasv_sock = s
        self.active_addr = None
        return s.getsockname()[1]

    def _pasv(self, arg):
        port = self._open_passive()
        ip = self.conn.getsockname()[0]     # the address this client reached us on
        h = ip.split(".")
        if len(h) != 4:
            return self.reply(425, "PASV needs IPv4 — use EPSV")
        self.reply(227, "Entering passive mode (%s,%d,%d)"
                   % (",".join(h), port >> 8, port & 0xFF))

    def _epsv(self, arg):
        port = self._open_passive()
        self.reply(229, f"Entering extended passive mode (|||{port}|)")

    def _port(self, arg):
        p = arg.split(",")
        if len(p) != 6:
            return self.reply(501, "Bad PORT")
        try:
            ip = ".".join(p[:4])
            port = (int(p[4]) << 8) + int(p[5])
        except ValueError:
            return self.reply(501, "Bad PORT")
        self._close_pasv()
        self.active_addr = (ip, port)
        self.reply(200, "PORT command successful")

    def _eprt(self, arg):
        # |2|::1|1234|  or  |1|10.0.0.1|1234|
        parts = arg.split("|")
        if len(parts) < 4:
            return self.reply(501, "Bad EPRT")
        try:
            port = int(parts[3])
        except ValueError:
            return self.reply(501, "Bad EPRT")
        self._close_pasv()
        self.active_addr = (parts[2], port)
        self.reply(200, "EPRT command successful")

    def _data(self):
        """Return the established data socket, or None (a reply is already sent)."""
        if self.pasv_sock:
            try:
                conn, _ = self.pasv_sock.accept()
            except (OSError, socket.timeout):
                self.reply(425, "No data connection was made")
                self._close_pasv()
                return None
            self._close_pasv()
        elif self.active_addr:
            try:
                conn = socket.create_connection(self.active_addr, timeout=30)
            except OSError as e:
                self.reply(425, f"Cannot open data connection: {e}")
                return None
        else:
            self.reply(425, "Use PASV or PORT first")
            return None
        conn.settimeout(120)
        return conn

    # -- listings ----------------------------------------------------------
    def _list(self, arg, long=True):
        v = self._virtual(arg)
        real = _safe_path(v)
        if os.path.isdir(real):
            names = sorted(os.listdir(real))
            entries = [(n, os.path.join(real, n)) for n in names]
        elif os.path.exists(real):
            entries = [(os.path.basename(real), real)]
        else:
            return self.reply(550, "No such file or directory")
        rows = []
        for name, path in entries:
            if long:
                rows.append(self._long_row(name, path))
            else:
                rows.append(name)
        body = ("\r\n".join(rows) + "\r\n") if rows else ""
        self.reply(150, "Here comes the directory listing")
        data = self._data()
        if not data:
            return
        try:
            data.sendall(body.encode("utf-8", "replace"))
        except OSError as e:
            return self.reply(426, f"Transfer failed: {e}")
        finally:
            data.close()
        self.reply(226, "Directory send OK")

    @staticmethod
    def _long_row(name, path):
        try:
            st = os.stat(path)
        except OSError:
            return f"----------   1 ascom ascom            0 Jan  1 00:00 {name}"
        isdir = os.path.isdir(path)
        perm = "drwxr-xr-x" if isdir else "-rw-r--r--"
        t = time.localtime(st.st_mtime)
        # within ~6 months clients expect a time, otherwise a year
        if abs(time.time() - st.st_mtime) < 180 * 86400:
            when = "%s %2d %02d:%02d" % (_MONTHS[t.tm_mon - 1], t.tm_mday,
                                         t.tm_hour, t.tm_min)
        else:
            when = "%s %2d  %4d" % (_MONTHS[t.tm_mon - 1], t.tm_mday, t.tm_year)
        return "%s   1 ascom    ascom %11d %s %s" % (perm, st.st_size, when, name)

    # -- transfers ---------------------------------------------------------
    def _retr(self, arg):
        real = self._real(arg)
        if not os.path.isfile(real):
            record("ftp", self.addr[0], "GET", self._virtual(arg), 0, False,
                   "not found")
            return self.reply(550, "No such file")
        self.reply(150, f"Opening {'BINARY' if self.binary else 'ASCII'} mode "
                        f"data connection for {os.path.basename(real)}")
        data = self._data()
        if not data:
            return
        sent = 0
        try:
            with open(real, "rb") as f:
                if self.rest:
                    f.seek(self.rest)
                    sent = self.rest
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    if not self.binary:
                        chunk = chunk.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
                    data.sendall(chunk)
                    sent += len(chunk)
        except OSError as e:
            record("ftp", self.addr[0], "GET", _rel(real), sent, False, str(e))
            self.reply(426, f"Transfer aborted: {e}")
            return
        finally:
            self.rest = 0
            data.close()
        record("ftp", self.addr[0], "GET", _rel(real), sent)
        self.reply(226, "Transfer complete")

    def _stor(self, arg, append=False):
        if not self._writable():
            record("ftp", self.addr[0], "PUT", self._virtual(arg), 0, False,
                   "uploads disabled")
            return
        real = self._real(arg)
        if os.path.isdir(real):
            return self.reply(550, "That is a directory")
        cap = max_upload_bytes()
        # what to restore the file to if the transfer fails part-way
        keep = os.path.getsize(real) if append and os.path.isfile(real) else 0
        self.reply(150, "Ready to receive data")
        data = self._data()
        if not data:
            return
        got = 0
        try:
            os.makedirs(os.path.dirname(real), exist_ok=True)
            with open(real, "ab" if append else "wb") as f:
                while True:
                    chunk = data.recv(65536)
                    if not chunk:
                        break
                    got += len(chunk)
                    if got > cap:
                        raise OSError(f"file exceeds the "
                                      f"{cap // (1024*1024)} MB limit")
                    if not self.binary:
                        chunk = chunk.replace(b"\r\n", b"\n")
                    f.write(chunk)
        except OSError as e:
            _discard_partial(real, keep)
            record("ftp", self.addr[0], "PUT", _rel(real), got, False, str(e))
            self.reply(426, f"Transfer failed: {e}")
            return
        finally:
            data.close()
        record("ftp", self.addr[0], "PUT", _rel(real), got)
        self.reply(226, "Transfer complete")

    def _rest(self, arg):
        try:
            self.rest = max(0, int(arg))
        except ValueError:
            return self.reply(501, "Bad REST")
        self.reply(350, f"Restarting at {self.rest}")

    def _size(self, arg):
        real = self._real(arg)
        if not os.path.isfile(real):
            return self.reply(550, "No such file")
        self.reply(213, str(os.path.getsize(real)))

    def _mdtm(self, arg):
        real = self._real(arg)
        if not os.path.exists(real):
            return self.reply(550, "No such file")
        self.reply(213, time.strftime("%Y%m%d%H%M%S",
                                      time.gmtime(os.path.getmtime(real))))

    def _dele(self, arg):
        if not self._writable():
            return
        real = self._real(arg)
        if not os.path.isfile(real):
            return self.reply(550, "No such file")
        os.remove(real)
        record("ftp", self.addr[0], "DELETE", _rel(real), 0)
        self.reply(250, "File deleted")

    def _mkd(self, arg):
        if not self._writable():
            return
        real = self._real(arg)
        os.makedirs(real, exist_ok=True)
        self.reply(257, f'"{self._virtual(arg)}" created')

    def _rmd(self, arg):
        if not self._writable():
            return
        real = self._real(arg)
        if not os.path.isdir(real):
            return self.reply(550, "No such directory")
        os.rmdir(real)
        self.reply(250, "Directory removed")

    def _rnfr(self, arg):
        if not self._writable():
            return
        real = self._real(arg)
        if not os.path.exists(real):
            return self.reply(550, "No such file")
        self.rename_from = real
        self.reply(350, "Ready for RNTO")

    def _rnto(self, arg):
        if not self._writable():
            return
        if not self.rename_from:
            return self.reply(503, "RNFR first")
        os.rename(self.rename_from, self._real(arg))
        self.rename_from = None
        self.reply(250, "Rename successful")


class FtpServer(_Server):
    proto = "ftp"

    def _start(self):
        self._sessions = set()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.cfg["bind"], int(self.cfg["port"])))
        self._sock.listen(8)
        self._sock.settimeout(0.5)
        self._stopping = False
        self._thread = threading.Thread(target=self._accept, daemon=True,
                                        name="fs-ftp")
        self._thread.start()

    def _accept(self):
        while not self._stopping:
            try:
                conn, addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if self._stopping:
                try:
                    conn.close()
                except OSError:
                    pass
                break
            if len(self._sessions) >= 32:
                try:
                    conn.sendall(b"421 Too many connections\r\n")
                    conn.close()
                except OSError:
                    pass
                continue
            s = _FtpSession(conn, addr, self.cfg, self)
            self._sessions.add(s)
            s.start()

    def forget(self, session):
        self._sessions.discard(session)

    def _stop(self):
        self._stopping = True
        # The accept thread is sitting in accept() with a 0.5 s timeout. Closing
        # the socket here does not wake it, and until it returns the port is
        # still held — so a settings change would fail to rebind. Wait for it.
        self._thread.join(timeout=3)
        try:
            self._sock.close()
        except OSError:
            pass
        for s in list(self._sessions):
            s.quit = True
            try:
                s.conn.close()
            except OSError:
                pass
        self._sessions.clear()


# --------------------------------------------------------------------------
# TFTP  (RFC 1350, with the option extension of RFC 2347-2349)
# --------------------------------------------------------------------------

_RRQ, _WRQ, _DATA, _ACK, _ERROR, _OACK = 1, 2, 3, 4, 5, 6
_TFTP_ERRORS = {0: "not defined", 1: "file not found", 2: "access violation",
                3: "disk full", 4: "illegal operation", 5: "unknown transfer id",
                6: "file already exists", 8: "option refused"}


class TftpServer(_Server):
    """UDP, one thread per transfer.

    TFTP has no login and no encryption — anyone who can reach the port can
    read the folder. That is exactly why it is off by default and why the page
    says so out loud.
    """
    proto = "tftp"

    def _start(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.cfg["bind"], int(self.cfg["port"])))
        self._sock.settimeout(0.5)
        self._stopping = False
        self._thread = threading.Thread(target=self._listen, daemon=True,
                                        name="fs-tftp")
        self._thread.start()

    def _stop(self):
        self._stopping = True
        self._thread.join(timeout=3)     # let recvfrom() release the port first
        try:
            self._sock.close()
        except OSError:
            pass

    def _listen(self):
        while not self._stopping:
            try:
                pkt, addr = self._sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(pkt, addr),
                             daemon=True, name="fs-tftp-xfer").start()

    # -- request parsing ---------------------------------------------------
    def _handle(self, pkt, addr):
        if len(pkt) < 4:
            return
        op = struct.unpack("!H", pkt[:2])[0]
        if op not in (_RRQ, _WRQ):
            return                       # stray packet for a finished transfer
        fields = pkt[2:].split(b"\x00")
        if len(fields) < 2:
            return
        filename = fields[0].decode("utf-8", "replace")
        mode = fields[1].decode("ascii", "replace").lower()
        opts = {}
        rest = [f for f in fields[2:] if f != b""]
        for i in range(0, len(rest) - 1, 2):
            opts[rest[i].decode("ascii", "replace").lower()] = \
                rest[i + 1].decode("ascii", "replace")

        # every transfer answers from its own ephemeral port, as the RFC requires
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((self.cfg["bind"], 0))
        try:
            if op == _RRQ:
                self._send_file(sock, addr, filename, mode, opts)
            else:
                self._recv_file(sock, addr, filename, mode, opts)
        except Exception as e:            # noqa: BLE001 - never kill the listener
            log.debug("tftp transfer error: %s", e)
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def _error(self, sock, addr, code, msg=""):
        msg = msg or _TFTP_ERRORS.get(code, "error")
        sock.sendto(struct.pack("!HH", _ERROR, code) + msg.encode() + b"\x00", addr)

    @staticmethod
    def _negotiate(opts, filesize=None):
        """Answer only the options we actually honour (RFC 2347: silence = no)."""
        out = {}
        blksize = 512
        timeout = 3
        if "blksize" in opts:
            try:
                blksize = max(8, min(65464, int(opts["blksize"])))
                out["blksize"] = str(blksize)
            except ValueError:
                pass
        if "timeout" in opts:
            try:
                timeout = max(1, min(255, int(opts["timeout"])))
                out["timeout"] = str(timeout)
            except ValueError:
                pass
        if "tsize" in opts and filesize is not None:
            out["tsize"] = str(filesize)
        return out, blksize, timeout

    @staticmethod
    def _oack(opts):
        body = b""
        for k, v in opts.items():
            body += k.encode() + b"\x00" + v.encode() + b"\x00"
        return struct.pack("!H", _OACK) + body

    # -- read (device downloads from us) -----------------------------------
    def _send_file(self, sock, addr, filename, mode, opts):
        try:
            real = _safe_path(filename)
        except ValueError:
            record("tftp", addr[0], "GET", filename, 0, False, "path refused")
            return self._error(sock, addr, 2)
        if not os.path.isfile(real):
            record("tftp", addr[0], "GET", filename, 0, False, "not found")
            return self._error(sock, addr, 1)

        size = os.path.getsize(real)
        ack, blksize, timeout = self._negotiate(opts, size)
        sock.settimeout(timeout)
        netascii = mode == "netascii"

        with open(real, "rb") as f:
            data = None
            if netascii:
                # small config files in practice; converting up front keeps the
                # block maths honest after the size changes
                data = f.read().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
                size = len(data)

            if ack:
                if not self._await(sock, addr, self._oack(ack), 0, timeout):
                    record("tftp", addr[0], "GET", _rel(real), 0, False,
                           "no reply to option ack")
                    return

            block, sent, offset = 1, 0, 0
            while True:
                if data is not None:
                    chunk = data[offset:offset + blksize]
                    offset += len(chunk)
                else:
                    chunk = f.read(blksize)
                pkt = struct.pack("!HH", _DATA, block) + chunk
                if not self._await(sock, addr, pkt, block, timeout):
                    record("tftp", addr[0], "GET", _rel(real), sent, False,
                           f"timed out at block {block}")
                    return
                sent += len(chunk)
                if len(chunk) < blksize:
                    break
                block = (block + 1) & 0xFFFF        # wraps to 0 past 65535
        record("tftp", addr[0], "GET", _rel(real), sent)

    def _await(self, sock, addr, pkt, block, timeout, retries=5):
        """Send a packet and wait for its ACK, retransmitting on silence."""
        for _ in range(retries):
            try:
                sock.sendto(pkt, addr)
            except OSError:
                return False
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    sock.settimeout(max(0.1, deadline - time.time()))
                    reply, raddr = sock.recvfrom(1024)
                except socket.timeout:
                    break
                except OSError:
                    return False
                if raddr != addr or len(reply) < 4:
                    continue
                op, got = struct.unpack("!HH", reply[:4])
                if op == _ERROR:
                    return False
                if op == _ACK and got == block:
                    return True
            if self._stopping:
                return False
        return False

    # -- write (device uploads to us) --------------------------------------
    def _recv_file(self, sock, addr, filename, mode, opts):
        if not self.cfg.get("upload"):
            record("tftp", addr[0], "PUT", filename, 0, False, "uploads disabled")
            return self._error(sock, addr, 2, "uploads are disabled on this server")
        try:
            real = _safe_path(filename)
        except ValueError:
            record("tftp", addr[0], "PUT", filename, 0, False, "path refused")
            return self._error(sock, addr, 2)
        if os.path.isdir(real):
            return self._error(sock, addr, 2, "that is a folder")

        ack, blksize, timeout = self._negotiate(opts)
        ack.pop("tsize", None)
        if "tsize" in opts:
            ack["tsize"] = opts["tsize"]          # echo the client's own figure
        sock.settimeout(timeout)
        netascii = mode == "netascii"
        cap = max_upload_bytes()

        first = self._oack(ack) if ack else struct.pack("!HH", _ACK, 0)
        expect = 1
        got = 0
        failed = ""
        try:
            os.makedirs(os.path.dirname(real), exist_ok=True)
            with open(real, "wb") as f:
                pkt = first
                block_to_ack = 0
                while True:
                    chunk = self._await_data(sock, addr, pkt, expect, timeout)
                    if chunk is None:
                        failed = f"timed out at block {expect}"
                        record("tftp", addr[0], "PUT", _rel(real), got, False,
                               failed)
                        break
                    got += len(chunk)
                    if got > cap:
                        self._error(sock, addr, 3, "file exceeds the size limit")
                        record("tftp", addr[0], "PUT", _rel(real), got, False,
                               "too large")
                        failed = "too large"
                        break
                    f.write(chunk.replace(b"\r\n", b"\n") if netascii else chunk)
                    block_to_ack = expect
                    pkt = struct.pack("!HH", _ACK, block_to_ack)
                    last = len(chunk) < blksize
                    if last:
                        sock.sendto(pkt, addr)
                        break
                    expect = (expect + 1) & 0xFFFF
        except OSError as e:
            _discard_partial(real)
            self._error(sock, addr, 3, str(e))
            record("tftp", addr[0], "PUT", _rel(real), got, False, str(e))
            return
        if failed:
            # never leave a truncated firmware image behind for the next device
            _discard_partial(real)
            return
        record("tftp", addr[0], "PUT", _rel(real), got)

    def _await_data(self, sock, addr, pkt, block, timeout, retries=5):
        """Send an ACK/OACK and wait for the DATA block that should follow."""
        for _ in range(retries):
            try:
                sock.sendto(pkt, addr)
            except OSError:
                return None
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    sock.settimeout(max(0.1, deadline - time.time()))
                    reply, raddr = sock.recvfrom(65536)
                except socket.timeout:
                    break
                except OSError:
                    return None
                if raddr != addr or len(reply) < 4:
                    continue
                op, got = struct.unpack("!HH", reply[:4])
                if op == _ERROR:
                    return None
                if op == _DATA and got == block:
                    return reply[4:]
                # a repeat of the previous block means our ACK was lost: re-send
                # it and keep waiting rather than starting the retry clock again
                if op == _DATA and got == ((block - 1) & 0xFFFF):
                    try:
                        sock.sendto(pkt, addr)
                    except OSError:
                        return None
            if self._stopping:
                return None
        return None


# --------------------------------------------------------------------------
# manager: keeps the running servers matching the settings
# --------------------------------------------------------------------------

_CLASSES = {"http": HttpServer, "https": HttpsServer,
            "ftp": FtpServer, "tftp": TftpServer}

_running = {}
_lock = threading.RLock()


def config(proto):
    """The settings for one protocol, as the server object wants them."""
    g = settings.get
    cfg = {"bind": (g("fs_bind") or "0.0.0.0").strip() or "0.0.0.0",
           "port": int(g(f"fs_{proto}_port")),
           "upload": bool(g(f"fs_{proto}_upload"))}
    if proto in ("http", "https"):
        cfg["user"] = g("fs_http_user")
        cfg["password"] = g("fs_http_pass")
    if proto == "https":
        cfg["cert"] = g("fs_https_cert")
        cfg["key"] = g("fs_https_key")
    if proto == "ftp":
        cfg["user"] = g("fs_ftp_user")
        cfg["password"] = g("fs_ftp_pass")
        cfg["anonymous"] = bool(g("fs_ftp_anonymous"))
        cfg["pasv_from"] = int(g("fs_ftp_pasv_from"))
        cfg["pasv_to"] = int(g("fs_ftp_pasv_to"))
    return cfg


def apply():
    """Start, stop and restart servers so reality matches the settings.

    Called at boot and after every settings save. A server whose configuration
    has not changed is left alone — restarting it would drop a firmware
    transfer that happens to be in flight while someone edits an unrelated box.
    """
    root = root_dir()
    with _lock:
        for proto in PROTOCOLS:
            want = bool(settings.get(f"fs_{proto}_enabled"))
            cfg = config(proto)
            cfg["root"] = root
            cur = _running.get(proto)
            if cur and (not want or cur.cfg != cfg):
                cur.stop()
                _running.pop(proto, None)
                cur = None
            if want and not cur:
                srv = _CLASSES[proto](cfg)
                srv.start()
                _running[proto] = srv       # kept even on failure, to show the error
    return status()


def stop_all():
    with _lock:
        for proto, srv in list(_running.items()):
            srv.stop()
            _running.pop(proto, None)


def status():
    out = {}
    with _lock:
        for proto in PROTOCOLS:
            srv = _running.get(proto)
            cfg = config(proto)
            out[proto] = {
                "enabled": bool(settings.get(f"fs_{proto}_enabled")),
                "running": bool(srv and srv.running),
                "error": srv.error if srv else "",
                "port": cfg["port"],
                "upload": cfg["upload"],
                "since": srv.started if srv and srv.running else 0,
            }
    out["root"] = root_dir(create=False)
    out["bind"] = (settings.get("fs_bind") or "0.0.0.0").strip()
    out["addresses"] = sorted(_local_ips())
    out["max_upload_mb"] = int(settings.get("fs_max_upload_mb"))
    return out


# --------------------------------------------------------------------------
# folder browsing for the GUI (the same root, over the web session)
# --------------------------------------------------------------------------

def list_dir(rel=""):
    """Files and folders under the served root, for the Tools page."""
    root = root_dir()
    real = _safe_path(rel, root)
    if not os.path.isdir(real):
        raise FileNotFoundError(rel)
    items = []
    for name in sorted(os.listdir(real), key=lambda n: n.lower()):
        p = os.path.join(real, name)
        try:
            st = os.stat(p)
        except OSError:
            continue
        items.append({"name": name, "dir": os.path.isdir(p),
                      "size": 0 if os.path.isdir(p) else st.st_size,
                      "mtime": st.st_mtime})
    items.sort(key=lambda i: (not i["dir"], i["name"].lower()))
    cur = "/" + os.path.relpath(real, os.path.realpath(root)).replace("\\", "/")
    return {"path": "/" if cur in ("/.", "/") else cur, "items": items,
            "root": root, "free": _free_space(root)}


def _free_space(path):
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return 0


def save_upload(rel_dir, filename, fileobj):
    """Store a browser upload into the served folder."""
    name = os.path.basename(filename or "").strip()
    if not name:
        raise ValueError("no filename")
    dest = _safe_path(os.path.join(rel_dir or "", name))
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    fileobj.save(dest)
    record("gui", "browser", "PUT", _rel(dest),
           os.path.getsize(dest) if os.path.exists(dest) else 0)
    return _rel(dest)


def delete(rel):
    target = _safe_path(rel)
    if target == os.path.realpath(root_dir()):
        raise ValueError("cannot delete the served folder itself")
    if os.path.isdir(target):
        shutil.rmtree(target)
    elif os.path.exists(target):
        os.remove(target)
    else:
        raise FileNotFoundError(rel)
    record("gui", "browser", "DELETE", "/" + rel.strip("/"), 0)


def make_dir(rel, name):
    name = os.path.basename((name or "").strip())
    if not name:
        raise ValueError("no folder name")
    target = _safe_path(os.path.join(rel or "", name))
    os.makedirs(target, exist_ok=True)
    return _rel(target)


def resolve_for_download(rel):
    """(directory, filename) for send_from_directory, checked against the root."""
    real = _safe_path(rel)
    if not os.path.isfile(real):
        raise FileNotFoundError(rel)
    return os.path.dirname(real), os.path.basename(real)
