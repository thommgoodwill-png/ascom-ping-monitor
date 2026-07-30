"""Child-process launching that never flashes a console window.

The Windows build is a PyInstaller ``--noconsole`` exe. A GUI process has no
console of its own, so when it starts a console program (ping, sc, tracert,
tcpdump...) Windows allocates a *new* console for the child — which the user
sees as a cmd box popping open and closing again, once per call. Because the
monitor runs these on a timer, that becomes a flashing window every few
seconds.

CREATE_NO_WINDOW suppresses that console. STARTF_USESHOWWINDOW/SW_HIDE is
belt-and-braces for the handful of hosts where a launcher hands the process a
console anyway.

Every subprocess in this codebase must go through here — never call
``subprocess.run``/``Popen`` directly.
"""
import os
import subprocess

IS_WINDOWS = os.name == "nt"
CREATE_NO_WINDOW = 0x08000000


def hidden_kwargs():
    """Keyword args that keep a child process's window off the screen."""
    if not IS_WINDOWS:
        return {}
    kw = {"creationflags": CREATE_NO_WINDOW}
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0                      # SW_HIDE
        kw["startupinfo"] = si
    except AttributeError:                      # non-Windows Python build
        pass
    return kw


def _merge(kw):
    hidden = hidden_kwargs()
    if not hidden:
        return kw
    kw = dict(kw)
    kw["creationflags"] = kw.get("creationflags", 0) | CREATE_NO_WINDOW
    kw.setdefault("startupinfo", hidden.get("startupinfo"))
    if kw["startupinfo"] is None:
        kw.pop("startupinfo")
    return kw


def run(cmd, **kw):
    """subprocess.run with the console window suppressed."""
    return subprocess.run(cmd, **_merge(kw))


def popen(cmd, **kw):
    """subprocess.Popen with the console window suppressed."""
    return subprocess.Popen(cmd, **_merge(kw))
