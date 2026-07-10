import asyncio
import ctypes
import os
import subprocess
import sys

try:
    import psutil
    import keyboard
    import websockets
    import rich
except ImportError:
    print("Missing required packages. Installing from requirements.txt...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"])
    os.execv(sys.executable, [sys.executable] + sys.argv)

import socket
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


INSTANCE_PORT = 48201

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
    def __init__(self, config, instance_socket=None):
        self.config = config
        self.username = config["username"]
        self.uuid = config["uuid"]
        self.server_ip = config["server_ip"]
        self.port = config["port"]
        self.password = config["password"]
        self.server_mode = "normal"

        self.game_running = False
        self.hidden = False
        self.hwnd = ctypes.windll.kernel32.GetConsoleWindow() if os.name == 'nt' else None
        self.instance_socket = instance_socket

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
        if self.paused and cause != "targeted":
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

    def action_rebind_panic(self):
        """Blocks the input thread while the user presses a new combo."""
        self.rebinding = True
        self.set_status("REBIND: release keys, then press the combo you want...")
        deadline = time.time() + 3.0
        while time.time() < deadline:
            try:
                held = any(keyboard.is_pressed(k) for k in ("k", "ctrl", "shift", "alt"))
            except Exception:
                held = False
            if not held:
                break
            time.sleep(0.03)
        time.sleep(0.12)
        try:
            keyboard.remove_hotkey(self.panic_keybind)
        except Exception:
            pass
        try:
            new_key = keyboard.read_hotkey(suppress=False)
        except Exception:
            new_key = None
            
        forbidden = ["a", "d", "p", "q", "h", "k", "1", "2", "3", "4", "5"]
        if new_key and (new_key.lower() in forbidden or new_key.lower() == "ctrl+shift+h"):
            self.set_status(f"invalid hotkey '{new_key}' (reserved). Try again.")
            self._register_panic()
            self.rebinding = False
            return
            
        if new_key:
            self.panic_keybind = new_key
            self._register_panic()
            self.save()
            self.set_status(f"panic key is now [{new_key}]")
        else:
            self._register_panic()
        self.rebinding = False

    def action_quit(self):
        self.set_status("quitting...")
        self.stop.set()
        self.action_disarm()
        if hasattr(self, "_gather_task"):
            self.loop.call_soon_threadsafe(self._gather_task.cancel)

    def action_hide(self):
        if not self.hwnd: return
        if not getattr(self, "unhide_bound", True):
            self.set_status("Cannot hide: unhide hotkey (ctrl+shift+h) failed to bind.")
            return
        self.hidden = True
        ctypes.windll.user32.ShowWindow(self.hwnd, 0)

    def toggle_visibility(self):
        if not self.hwnd: return
        self.hidden = not self.hidden
        ctypes.windll.user32.ShowWindow(self.hwnd, 1 if not self.hidden else 0)

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

    async def game_status_loop(self):
        """Poll psutil to check if GTA5 is running."""
        while not self.stop.is_set():
            running = False
            for p in psutil.process_iter(['name']):
                try:
                    if p.info['name'] and p.info['name'].lower().startswith("gta5"):
                        running = True
                        break
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            
            if running != self.game_running:
                self.game_running = running
                self.send_ts({"type": "game_status", "running": running})
            await asyncio.sleep(3)

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
            self.set_status(f"KICKED by server — {data.get('reason','')}.")
        elif mtype == "settings_override":
            self.settings = normalize_settings(data.get("settings"))
            self.save()
            self.set_status("settings changed by server operator.")
        elif mtype == "countdown":
            self._countdown_task = self.loop.create_task(self._countdown(int(data.get("seconds", 5))))
        elif mtype == "notice":
            self.set_status(f"server: {data.get('msg','')}")
        elif mtype == "force_arm":
            self.armed = data.get("armed", False)
            self.set_status(f"Server forced you to {'ARM' if self.armed else 'DISARM'}.")
            if self.armed:
                self.timeout_killed = False
        elif mtype == "force_pause":
            self.paused = data.get("paused", False)
            self.set_status(f"Server forced you to {'PAUSE' if self.paused else 'UNPAUSE'}.")
            if not self.paused:
                self.timeout_killed = False
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
                    self.timeout_killed = False
                    await self._send({
                        "type": "hello", "username": self.username, "uuid": self.uuid,
                        "password": self.password, "version": PROTOCOL_VERSION,
                        "settings": self.settings, "game_running": self.game_running,
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
        if self.server_mode == "safe":
            table.add_column("Safe", justify="center")
        table.add_column("Game", justify="center")

        def mark(v):
            return Text("✓", style="green") if v else Text("·", style="dim")

        roster = self.roster or [{
            "username": self.username, "connected": self.connected,
            "armed": self.armed, "stale": False, "settings": self.settings,
            "uuid": self.uuid, "game_running": self.game_running,
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
            if not c.get("connected"):
                game = Text("—", style="dim")
            else:
                game = Text("RUNNING", style="bold green") if c.get("game_running") else Text("off", style="dim")
            s = c.get("settings", {})
            row = [name, cs, armed] + [mark(s.get(key)) for key, _ in SETTING_COLS]
            if self.server_mode == "safe":
                row.append(Text("SAFE", style="yellow"))
            row.append(game)
            table.add_row(*row)

        legend = ("[A]rm  [D]isarm  [P]ause  "
                  "[1]DiscKill [2]IgnDisc [3]IgnSrv [4]IgnPanic [5]Dry  "
                  "[K] Rebind panic  [H]ide  [Q]uit")
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
        with Live(console=console, refresh_per_second=8, screen=False) as live:
            while not self.stop.is_set():
                try:
                    live.update(self.render())
                    await asyncio.sleep(0.15)
                except asyncio.CancelledError:
                    break

    def listen_for_unhide(self):
        if not self.instance_socket:
            return
        while not self.stop.is_set():
            try:
                self.instance_socket.settimeout(1.0)
                data, _ = self.instance_socket.recvfrom(1024)
                if data == b"UNHIDE":
                    if self.hidden:
                        self.toggle_visibility()
            except socket.timeout:
                continue
            except Exception:
                continue

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
                self.action_rebind_panic()
            elif c == "h":
                self.action_hide()
            elif c == "q":
                self.action_quit()
                break

    # ---- entry -------------------------------------------------------------
    async def run(self):
        self.loop = asyncio.get_running_loop()
        self.settings = normalize_settings(self.settings)

        # Disable window Close button (Alt-F4/X)
        if self.hwnd:
            hmenu = ctypes.windll.user32.GetSystemMenu(self.hwnd, False)
            if hmenu:
                ctypes.windll.user32.EnableMenuItem(hmenu, 0xF060, 1) # SC_CLOSE, MF_GRAYED

        keyboard.add_hotkey(self.panic_keybind, self.do_panic)
        try:
            keyboard.add_hotkey("ctrl+shift+h", self.toggle_visibility)
            self.unhide_bound = True
        except Exception as e:
            self.set_status(f"Warning: couldn't bind unhide key (ctrl+shift+h): {e}")
            self.unhide_bound = False

        threading.Thread(target=self.input_loop, daemon=True).start()
        if self.instance_socket:
            threading.Thread(target=self.listen_for_unhide, daemon=True).start()
            
        self.set_status(f"Ready. Press [A] to arm. Panic key: {self.panic_keybind}")
        try:
            self._gather_task = asyncio.gather(
                self.connection_loop(), 
                self.watchdog_loop(), 
                self.game_status_loop(),
                self.tui_loop()
            )
            await self._gather_task
        except asyncio.CancelledError:
            pass
        finally:
            try:
                keyboard.remove_hotkey(self.panic_keybind)
                keyboard.remove_hotkey("ctrl+shift+h")
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
    
    if config.get("password") == "changeme":
        pw = input("Shared password (press Enter if host removed the password): ").strip()
        if pw:
            config["password"] = pw
            changed = True

    if changed:
        save_config(CONFIG_FILE, config)


def main():
    if not SKIP_ADMIN and os.name == "nt" and not ctypes.windll.shell32.IsUserAnAdmin():
        print("Please run as ADMINISTRATOR so the panic hotkey works over GTA5.")
        input("Press Enter to exit...")
        sys.exit(1)

    import socket
    instance_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        instance_socket.bind(('127.0.0.1', INSTANCE_PORT))
    except OSError:
        # Port is in use, another instance is running.
        instance_socket.sendto(b"UNHIDE", ('127.0.0.1', INSTANCE_PORT))
        sys.exit(99)

    config = load_config(CONFIG_FILE, CLIENT_DEFAULTS)
    first_run_setup(config)

    while True:
        client = Client(config, instance_socket)
        try:
            asyncio.run(client.run())
        except KeyboardInterrupt:
            break
            
        if getattr(client, "auth_failed_reason", None):
            os.system('cls' if os.name == 'nt' else 'clear')
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
