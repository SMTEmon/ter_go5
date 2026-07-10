"""
GTA Heist Sync - Client

A long-lived, never-auto-exiting client for the panic-kill mesh.

- Live dashboard (rich) of everyone connected and their toggles.
- Single-key menu: arm, disarm, pause, flip your own toggles, rebind panic key.
- Reconnects on its own; a kill just drops you back to a ready state.
- The server is authoritative over settings and can override yours anytime.
"""
import asyncio
import ctypes
import os
import subprocess
import sys
import threading
import time
from datetime import datetime

import keyboard
import websockets
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from common import (
    PROTOCOL_VERSION, HEARTBEAT_INTERVAL, SERVER_TIMEOUT,
    DEFAULT_SETTINGS, SETTINGS_HELP, normalize_settings,
    load_config, save_config, CLIENT_DEFAULTS, make_getch,
)

CONFIG_FILE = "client_config.json"
SKIP_ADMIN = False
for arg in sys.argv[1:]:
    if arg == "--demo":
        SKIP_ADMIN = True
    elif arg.endswith(".json"):
        CONFIG_FILE = arg

# The dashboard uses a few Unicode glyphs; make sure the Windows console can
# encode them even under a legacy cp1252 code page.
if os.name == "nt":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

console = Console()

# Short column labels for the dashboard, in display order.
SETTING_COLS = [
    ("disconnect_kill", "DiscKill"),
    ("ignore_disconnect_kills", "IgnDisc"),
    ("ignore_server_timeout_kills", "IgnSrv"),
    ("ignore_other_panic", "IgnPanic"),
    ("dry_run", "Dry"),
]
# Number-key -> setting, for the local menu.
TOGGLE_KEYS = {
    "1": "disconnect_kill",
    "2": "ignore_disconnect_kills",
    "3": "ignore_server_timeout_kills",
    "4": "ignore_other_panic",
    "5": "dry_run",
}


def kill_processes():
    """Actually close GTA5 (Enhanced + BattlEye variants)."""
    names = ["GTA5_Enhanced.exe", "GTA5_Enhanced_BE.exe", "GTA5.exe", "PlayGTAV.exe"]
    for name in names:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/IM", name],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.run(["pkill", "-9", name],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Client:
    def __init__(self, config):
        self.config = config
        self.username = config["username"]
        self.uuid = config["uuid"]
        self.server_ip = config["server_ip"]
        self.port = config["port"]
        self.password = config["password"]
        self.panic_keybind = config.get("panic_keybind", "ctrl+shift+f12")
        self.settings = normalize_settings(config.get("settings"))

        self.loop = None
        self.websocket = None
        self.connected = False
        self.armed = False
        self.paused = False
        self.rebinding = False
        self.stop = threading.Event()

        self.roster = []                       # last roster from server
        self.server_mode = "normal"
        self.last_server_msg = time.monotonic()
        self.timeout_killed = False            # avoid repeated server-timeout kills
        self.status = "Starting up..."

    # ---- status / persistence ---------------------------------------------
    def set_status(self, msg):
        self.status = f"[{datetime.now():%H:%M:%S}] {msg}"

    def save(self):
        self.config["settings"] = self.settings
        self.config["panic_keybind"] = self.panic_keybind
        self.config["server_ip"] = self.server_ip
        self.config["password"] = self.password
        save_config(CONFIG_FILE, self.config)

    # ---- outbound ----------------------------------------------------------
    async def _send(self, msg):
        if self.websocket and self.connected:
            try:
                await self.websocket.send(_json(msg))
            except Exception:
                pass

    def send_ts(self, msg):
        if self.loop:
            asyncio.run_coroutine_threadsafe(self._send(msg), self.loop)

    # ---- kill --------------------------------------------------------------
    def execute_kill(self, reason):
        if self.settings.get("dry_run"):
            self.set_status(f"[DRY RUN] would kill GTA — {reason}")
            return
        kill_processes()
        self.set_status(f"*** KILLED GTA — {reason} ***")

    def handle_kill(self, cause, reason):
        if self.paused:
            self.set_status(f"(paused) ignored kill [{cause}] — {reason}")
            return
        ignore = {
            "disconnect": "ignore_disconnect_kills",
            "panic": "ignore_other_panic",
        }.get(cause)
        if ignore and self.settings.get(ignore):
            self.set_status(f"ignored kill [{cause}] via toggle — {reason}")
            return
        self.execute_kill(reason)
        self.armed = False  # drop to ready state; re-arm to rejoin

    # ---- menu actions (called from the input thread) ----------------------
    def action_arm(self):
        if not self.connected:
            self.set_status("can't arm — not connected.")
            return
        self.armed = True
        self.timeout_killed = False
        self.send_ts({"type": "arm"})
        self.set_status("ARMED — you're in the run.")

    def action_disarm(self):
        self.armed = False
        self.send_ts({"type": "disarm"})
        self.set_status("disarmed — opted out.")

    def action_pause(self):
        self.paused = not self.paused
        self.set_status("PAUSED — ignoring all kills." if self.paused else "resumed.")

    def action_toggle(self, key):
        self.settings[key] = not self.settings.get(key)
        self.save()
        self.send_ts({"type": "settings_update", "settings": self.settings})
        self.set_status(f"{key} -> {'on' if self.settings[key] else 'off'}")

    def action_rebind(self):
        """Blocks the input thread while the user presses a new combo."""
        self.rebinding = True
        self.set_status("REBIND: release keys, then press the combo you want...")
        # The 'k' that opened this is almost certainly still physically down;
        # if we record now, read_hotkey grabs 'k'. Wait for a released keyboard.
        deadline = time.time() + 3.0
        while time.time() < deadline:
            try:
                held = any(keyboard.is_pressed(k) for k in ("k", "ctrl", "shift", "alt"))
            except Exception:
                held = False
            if not held:
                break
            time.sleep(0.03)
        time.sleep(0.12)  # debounce settle so a fast tap of 'k' clears too
        try:
            new_key = keyboard.read_hotkey(suppress=False)
        except Exception:
            new_key = None
        if new_key:
            try:
                keyboard.remove_hotkey(self.panic_keybind)
            except Exception:
                pass
            self.panic_keybind = new_key
            self._register_panic()
            self.save()
            self.set_status(f"panic key is now [{new_key}]")
        self.rebinding = False

    def action_quit(self):
        self.set_status("quitting...")
        self.stop.set()
        self.action_disarm()

    def do_panic(self):
        self.set_status("PANIC pressed!")
        self.execute_kill("local panic")
        self.send_ts({"type": "panic"})
        self.armed = False

    def _register_panic(self):
        try:
            keyboard.add_hotkey(self.panic_keybind, self.do_panic)
        except Exception as e:
            self.set_status(f"couldn't bind panic key: {e}")

    # ---- inbound -----------------------------------------------------------
    def handle_server_message(self, data):
        self.last_server_msg = time.monotonic()
        mtype = data.get("type")
        if mtype == "welcome":
            self.settings = normalize_settings(data.get("settings"))
            self.server_mode = data.get("server_mode", "normal")
            self.save()
            self.set_status("connected — welcome received.")
        elif mtype == "roster":
            self.roster = data.get("clients", [])
            self.server_mode = data.get("server_mode", "normal")
        elif mtype == "ping":
            pass
        elif mtype == "kill":
            self.handle_kill(data.get("cause", "?"), data.get("reason", ""))
        elif mtype == "kicked":
            self.armed = False
            self.set_status(f"KICKED by server — {data.get('reason','')}. Press [A] to re-arm.")
        elif mtype == "settings_override":
            self.settings = normalize_settings(data.get("settings"))
            self.save()
            self.set_status("settings changed by server operator.")
        elif mtype == "countdown":
            self.loop.create_task(self._countdown(int(data.get("seconds", 5))))
        elif mtype == "notice":
            self.set_status(f"server: {data.get('msg','')}")
        elif mtype == "auth_failed":
            self.auth_failed_reason = data.get('reason','')
            self.stop.set()

    async def _countdown(self, seconds):
        for n in range(seconds, 0, -1):
            if self.paused:
                self.set_status("(paused) countdown ignored.")
                return
            self.set_status(f"SYNCHRONIZED KILL IN {n}...")
            await asyncio.sleep(1)
        self.execute_kill("synchronized countdown")
        self.armed = False

    # ---- async loops -------------------------------------------------------
    async def connection_loop(self):
        uri = f"ws://{self.server_ip}:{self.port}"
        while not self.stop.is_set():
            try:
                self.set_status(f"connecting to {uri} ...")
                async with websockets.connect(uri) as ws:
                    self.websocket = ws
                    self.connected = True
                    self.last_server_msg = time.monotonic()
                    await self._send({
                        "type": "hello", "username": self.username, "uuid": self.uuid,
                        "password": self.password, "version": PROTOCOL_VERSION,
                        "settings": self.settings,
                    })
                    # Re-announce armed state if we were armed before a drop.
                    if self.armed:
                        await self._send({"type": "arm"})
                    await asyncio.gather(self._listen(ws), self._heartbeat(ws))
            except Exception as e:
                self.set_status(f"disconnected ({type(e).__name__}); retrying...")
            self.connected = False
            self.websocket = None
            if self.stop.is_set():
                break
            await asyncio.sleep(1.0)

    async def _listen(self, ws):
        async for raw in ws:
            try:
                self.handle_server_message(_loads(raw))
            except Exception:
                pass

    async def _heartbeat(self, ws):
        while self.connected and not self.stop.is_set():
            try:
                await ws.send(_json({"type": "heartbeat"}))
            except Exception:
                break
            await asyncio.sleep(HEARTBEAT_INTERVAL)

    async def watchdog_loop(self):
        """Detect a dead/silent server and self-kill unless told to ignore."""
        while not self.stop.is_set():
            await asyncio.sleep(0.5)
            silent = time.monotonic() - self.last_server_msg
            if (self.armed and not self.paused and not self.timeout_killed
                    and silent > SERVER_TIMEOUT):
                self.timeout_killed = True
                if self.settings.get("ignore_server_timeout_kills"):
                    self.set_status("server silent — ignored (toggle on).")
                else:
                    self.execute_kill("server stopped responding")
                    self.armed = False

    # ---- dashboard ---------------------------------------------------------
    def render(self):
        conn = Text("● CONNECTED", style="bold green") if self.connected else Text("○ offline", style="bold red")
        mode = Text(f"server: {self.server_mode.upper()}",
                    style="bold yellow" if self.server_mode == "safe" else "dim")
        my_state = ("PAUSED" if self.paused else ("ARMED" if self.armed else "idle"))
        my_style = {"PAUSED": "bold magenta", "ARMED": "bold green", "idle": "dim"}[my_state]

        header = Table.grid(expand=True)
        header.add_column(justify="left")
        header.add_column(justify="right")
        header.add_row(
            Text.assemble((f"{self.username}  ", "bold cyan"), (f"[{my_state}]", my_style)),
            Text.assemble(conn, "   ", mode),
        )

        table = Table(expand=True, header_style="bold")
        table.add_column("Player")
        table.add_column("Conn", justify="center")
        table.add_column("Armed", justify="center")
        for _, label in SETTING_COLS:
            table.add_column(label, justify="center")

        def mark(v):
            return Text("✓", style="green") if v else Text("·", style="dim")

        roster = self.roster or [{
            "username": self.username, "connected": self.connected,
            "armed": self.armed, "stale": False, "settings": self.settings,
            "uuid": self.uuid,
        }]
        for c in roster:
            me = c.get("uuid") == self.uuid
            name = Text(c.get("username", "?") + (" (you)" if me else ""),
                        style="bold cyan" if me else "white")
            if not c.get("connected"):
                cs = Text("off", style="red")
            elif c.get("stale"):
                cs = Text("stale", style="yellow")
            else:
                cs = Text("on", style="green")
            armed = Text("ARM", style="bold green") if c.get("armed") else Text("—", style="dim")
            s = c.get("settings", {})
            row = [name, cs, armed] + [mark(s.get(key)) for key, _ in SETTING_COLS]
            table.add_row(*row)

        legend = ("[A]rm  [D]isarm  [P]ause  "
                  "[1]DiscKill [2]IgnDisc [3]IgnSrv [4]IgnPanic [5]Dry  "
                  "[K] Rebind panic  [Q]uit")
        panic = Text(f"Panic key: {self.panic_keybind}", style="bold red")
        status = Text(self.status, style="italic")

        body = Table.grid(expand=True)
        body.add_row(header)
        body.add_row(table)
        body.add_row(Text(legend, style="dim"))
        body.add_row(panic)
        body.add_row(status)
        title = "GTA Heist Sync" + ("   — REBINDING: press a combo —" if self.rebinding else "")
        return Panel(body, title=title, border_style="cyan")

    async def tui_loop(self):
        with Live(self.render(), console=console, refresh_per_second=8, screen=False) as live:
            while not self.stop.is_set():
                live.update(self.render())
                await asyncio.sleep(0.15)

    # ---- input thread ------------------------------------------------------
    def input_loop(self):
        getch = make_getch()
        while not self.stop.is_set():
            ch = getch()
            if ch is None:
                time.sleep(0.05)
                continue
            if self.rebinding:      # rebind grabs keys itself
                continue
            c = ch.lower()
            if c == "a":
                self.action_arm()
            elif c == "d":
                self.action_disarm()
            elif c == "p":
                self.action_pause()
            elif c in TOGGLE_KEYS:
                self.action_toggle(TOGGLE_KEYS[c])
            elif c == "k":
                self.action_rebind()
            elif c == "q":
                self.action_quit()
                break

    # ---- entry -------------------------------------------------------------
    async def run(self):
        self.loop = asyncio.get_running_loop()
        self._register_panic()
        threading.Thread(target=self.input_loop, daemon=True).start()
        self.set_status(f"Ready. Press [A] to arm. Panic key: {self.panic_keybind}")
        try:
            await asyncio.gather(self.connection_loop(), self.watchdog_loop(), self.tui_loop())
        finally:
            try:
                keyboard.remove_hotkey(self.panic_keybind)
            except Exception:
                pass


# ---- tiny json helpers (avoid importing json in three places) --------------
import json as _json_mod
def _json(obj): return _json_mod.dumps(obj)
def _loads(s): return _json_mod.loads(s)


def first_run_setup(config):
    """Prompt once for the essentials if the config is fresh."""
    changed = False
    if not config.get("username"):
        config["username"] = input("Choose a username: ").strip() or "Player"
        changed = True
    if config.get("server_ip") == "127.0.0.1":
        ip = input(f"Server IP [{config.get('server_ip')}]: ").strip()
        if ip:
            config["server_ip"] = ip
            changed = True
    if changed:
        save_config(CONFIG_FILE, config)


def main():
    if not SKIP_ADMIN and os.name == "nt" and not ctypes.windll.shell32.IsUserAnAdmin():
        print("Please run as ADMINISTRATOR so the panic hotkey works over GTA5.")
        input("Press Enter to exit...")
        sys.exit(1)

    config = load_config(CONFIG_FILE, CLIENT_DEFAULTS)
    first_run_setup(config)

    while True:
        client = Client(config)
        try:
            asyncio.run(client.run())
        except KeyboardInterrupt:
            break
            
        if getattr(client, "auth_failed_reason", None):
            print(f"\n[!] Connection rejected: {client.auth_failed_reason}")
            try:
                pw = input("Enter correct server password: ").strip()
                if pw:
                    config["password"] = pw
                    save_config(CONFIG_FILE, config)
            except (EOFError, KeyboardInterrupt):
                break
        else:
            break
            
    print("\nExited. Good luck out there!")


if __name__ == "__main__":
    main()
