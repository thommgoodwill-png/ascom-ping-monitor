"""Per-device service checks that run alongside ping: TCP ports, HTTP(S)
health + certificate expiry, and DNS resolution timing. All pure-Python
(socket/ssl/http), no external binaries, so they work from any container."""
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request


def tcp_check(host, port, timeout=3.0):
    """Try to open a TCP connection. Returns (ok, ms, detail)."""
    start = time.time()
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            ms = (time.time() - start) * 1000
            return True, round(ms, 1), "open"
    except socket.timeout:
        return False, None, "timeout"
    except ConnectionRefusedError:
        return False, None, "refused"
    except OSError as e:
        return False, None, str(e.strerror or e)


def dns_check(hostname, timeout=3.0):
    """Time a DNS resolution. Returns (ok, ms, detail=resolved ip)."""
    # skip if it's already a literal IP
    try:
        socket.inet_aton(hostname)
        return True, 0.0, hostname
    except OSError:
        pass
    old = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout)
    start = time.time()
    try:
        ip = socket.gethostbyname(hostname)
        ms = (time.time() - start) * 1000
        return True, round(ms, 1), ip
    except socket.gaierror as e:
        return False, None, "resolve failed"
    except socket.timeout:
        return False, None, "timeout"
    finally:
        socket.setdefaulttimeout(old)


def http_check(url, timeout=8.0):
    """GET a URL, return (ok, ms, status, cert_days_left, detail).

    ok is True for HTTP status < 400. cert_days_left is set for https.
    """
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    cert_days = None
    # certificate expiry (https only) — separate low-level probe
    if url.startswith("https://"):
        cert_days = _cert_days_left(url, timeout)
    start = time.time()
    try:
        req = urllib.request.Request(url, method="GET",
                                     headers={"User-Agent": "AscomPingMonitor"})
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE   # we report cert separately; don't fail on it
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            ms = (time.time() - start) * 1000
            code = resp.getcode()
            ok = code < 400
            return ok, round(ms, 1), code, cert_days, f"HTTP {code}"
    except urllib.error.HTTPError as e:
        ms = (time.time() - start) * 1000
        return e.code < 400, round(ms, 1), e.code, cert_days, f"HTTP {e.code}"
    except (urllib.error.URLError, ssl.SSLError, socket.timeout, OSError) as e:
        reason = getattr(e, "reason", e)
        return False, None, None, cert_days, str(reason)


def _cert_days_left(url, timeout):
    """Days until the TLS cert expires, or None. Uses a verifying context so
    getpeercert() returns the parsed dict (notAfter)."""
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
        port = parts.port or 443
        ctx = ssl.create_default_context()   # verifying context: parsed cert dict
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ss:
                info = ss.getpeercert()
        if info and info.get("notAfter"):
            exp = ssl.cert_time_to_seconds(info["notAfter"])
            return int((exp - time.time()) / 86400)
    except Exception:
        # verification may fail (self-signed/expired) — fall back to raw read
        try:
            ctx = ssl._create_unverified_context()
            with socket.create_connection((host, port), timeout=timeout) as sock:
                with ctx.wrap_socket(sock, server_hostname=host) as ss:
                    der = ss.getpeercert(binary_form=True)
            import cryptography.x509 as x509   # optional; skip if unavailable
            cert = x509.load_der_x509_certificate(der)
            return int((cert.not_valid_after_utc.timestamp() - time.time()) / 86400)
        except Exception:
            return None
    return None


def parse_ports(text):
    """'443, 22 ,3389' -> [443, 22, 3389] (deduped, valid, capped)."""
    out = []
    for tok in str(text or "").replace(";", ",").split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            p = int(tok)
        except ValueError:
            continue
        if 1 <= p <= 65535 and p not in out:
            out.append(p)
    return out[:12]
