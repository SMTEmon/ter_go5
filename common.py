"""
Both server.py and client.py import from here so the wire format and the
setting names can never drift apart.
"""
import json
import os
import uuid

# Bump this if the wire protocol changes in a breaking way. The server warns
# when a client connects with a mismatched major version.
PROTOCOL_VERSION = "2.0"

# ---- Timing (seconds) -------------------------------------------------------
HEARTBEAT_INTERVAL = 0.5   # how often the client pings the server
SERVER_PING_INTERVAL = 1.0 # how often the server sends a keepalive to clients
CLIENT_TIMEOUT = 2.5       # server marks a client "stale" after this silence
SERVER_TIMEOUT = 3.0       # client treats the server as dead after this silence
DEFAULT_GRACE = 1.0        # disconnect grace window before a disconnect-kill

# ---- Per-client settings ----------------------------------------------------
# These are the values the server treats as authoritative and can override for
# any client (individually or all at once). Booleans only, so the `set` CLI
# command stays trivial. panic_keybind is client-local (not server-set).
DEFAULT_SETTINGS = {
    "disconnect_kill": False,            # my drop should kill everyone (after grace)
    "ignore_disconnect_kills": False,    # don't kill my game on someone's disconnect
    "ignore_server_timeout_kills": False,# don't kill my game if the server goes silent
    "ignore_other_panic": False,         # don't kill my game on another player's panic
    "dry_run": False,                    # log kills instead of actually closing GTA
}

# Human-readable descriptions, shown in the dashboards / help.
SETTINGS_HELP = {
    "disconnect_kill": "My unexpected drop triggers a kill for everyone",
    "ignore_disconnect_kills": "Ignore kills caused by a player disconnecting",
    "ignore_server_timeout_kills": "Ignore kills caused by the server going silent",
    "ignore_other_panic": "Ignore kills caused by another player's panic",
    "dry_run": "Test mode: log kills instead of closing GTA",
}

BOOL_SETTINGS = list(DEFAULT_SETTINGS.keys())


def normalize_settings(raw):
    """Return a settings dict with every known key present and correctly typed."""
    out = dict(DEFAULT_SETTINGS)
    if isinstance(raw, dict):
        for key in DEFAULT_SETTINGS:
            if key in raw:
                out[key] = bool(raw[key])
    return out


def parse_bool(text):
    """Parse on/off/true/false/1/0/yes/no. Returns None if unrecognized."""
    t = str(text).strip().lower()
    if t in ("on", "true", "1", "yes", "y"):
        return True
    if t in ("off", "false", "0", "no", "n"):
        return False
    return None


# ---- Config file helpers ----------------------------------------------------

def _config_path(filename):
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)


def load_config(filename, defaults):
    """Load a JSON config next to the scripts, filling in any missing defaults.

    Missing keys are written back so the file is always complete and easy to
    hand-edit. Returns the merged dict.
    """
    path = _config_path(filename)
    data = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"[warn] {filename} was unreadable ({e}); starting from defaults.")
            data = {}

    merged = dict(defaults)
    merged.update(data)

    # Make sure a uuid exists once and never changes for this install.
    if "uuid" in defaults and not merged.get("uuid"):
        merged["uuid"] = str(uuid.uuid4())

    save_config(filename, merged)
    return merged


def save_config(filename, data):
    path = _config_path(filename)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError as e:
        print(f"[warn] Could not save {filename}: {e}")


def make_getch():
    """Return a non-blocking single-key reader for the current platform.

    Returns a callable that yields one character if a key is waiting, else None.
    Special/function keys are swallowed (returns None) so they don't misfire.
    """
    if os.name == "nt":
        import msvcrt

        def getch():
            if msvcrt.kbhit():
                ch = msvcrt.getch()
                if ch in (b"\x00", b"\xe0"):  # special key
                    ch2 = msvcrt.getch()
                    if ch2 == b"H": return "UP"
                    if ch2 == b"P": return "DOWN"
                    return None
                try:
                    return ch.decode(errors="ignore")
                except Exception:
                    return None
            return None
        return getch

    import select

    def getch():
        if select.select([__import__("sys").stdin], [], [], 0)[0]:
            return __import__("sys").stdin.read(1)
        return None
    return getch


CLIENT_DEFAULTS = {
    "username": "",
    "uuid": "",
    "server_ip": "127.0.0.1",
    "port": 8765,
    "password": "changeme",
    "settings": dict(DEFAULT_SETTINGS),
    "panic_keybind": "ctrl+shift+f12",
}

SERVER_DEFAULTS = {
    "host": "0.0.0.0",
    "port": 8765,
    "password": "changeme",
    "grace_seconds": DEFAULT_GRACE,
    # Remembered per-uuid settings so overrides survive a server restart.
    "saved_settings": {},
    "saved_names": {},
}
