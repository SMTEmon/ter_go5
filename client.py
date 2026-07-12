import asyncio
import ctypes
import os
import subprocess
import sys
import uuid

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
    MESH_PORT, MESH_EVENT_TTL,
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
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-10)
        mode = ctypes.c_uint32()
        kernel32.GetConsoleMode(handle, ctypes.byref(mode))
        mode.value &= ~0x0040
        mode.value |= 0x0080
        kernel32.SetConsoleMode(handle, mode)
    except Exception:
        pass

console = Console()

# Short column labels for the dashboard, in display order.
SETTING_COLS = [
    ("ignore_other_panic", "IgnPanic"),
    ("dry_run", "Dry"),
]
# Number-key -> setting, for the local menu.
TOGGLE_KEYS = {
    "1": "ignore_other_panic",
    "2": "dry_run",
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
        
        self.mesh_enabled = bool(config.get("mesh_enabled", True))
        self.mesh_port = int(config.get("mesh_port", MESH_PORT))
        self.mesh = None
        self.mode = "OFFLINE"
        self.kill_events = {}
        self.last_server_mode_safe = False

    # ---- status / persistence ---------------------------------------------
    def set_status(self, msg):
        self.status = f"[{datetime.now():%H:%M:%S}] {msg}"

    def save(self):
        self.config["settings"] = self.settings
        self.config["panic_keybind"] = self.panic_keybind
        self.config["server_ip"] = self.server_ip
        self.config["password"] = self.password
        if "known_peers" not in self.config:
            self.config["known_peers"] = {}
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

    def handle_kill(self, cause, reason, event_id=None):
        if event_id:
            now = time.monotonic()
            self.kill_events = {k: v for k, v in self.kill_events.items() if (now - v) <= MESH_EVENT_TTL}
            if event_id in self.kill_events:
                return
            self.kill_events[event_id] = now
            
        if not self.armed and cause != "targeted":
            self.set_status(f"(disarmed) ignored kill [{cause}] — {reason}")
            return
            
        if (self.paused or self.last_server_mode_safe) and cause != "targeted":
            prefix = "paused" if self.paused else "safe mode"
            self.set_status(f"({prefix}) ignored kill [{cause}] — {reason}")
            return
        ignore = {
            "panic": "ignore_other_panic",
        }.get(cause)
        if ignore and self.settings.get(ignore):
            self.set_status(f"ignored kill [{cause}] via toggle — {reason}")
            return
        self.execute_kill(reason)
        self.armed = False  # drop to ready state; re-arm to rejoin

    def on_mesh_kill(self, cause, reason, event_id, from_name):
        self.handle_kill(cause, f"{reason} (via mesh from {from_name})", event_id)

    # ---- menu actions (called from the input thread) ----------------------
    def action_arm(self):
        if getattr(self, "suspended", False):
            self.suspended = False
        if not self.connected and not (self.mesh and self.mesh.alive_peers()):
            self.set_status("can't arm — no server and no peers.")
            return
        self.armed = True
        self.timeout_killed = False
        self.send_ts({"type": "arm"})
        self.set_status("ARMED — you're in the run.")

    def action_disarm(self):
        self.armed = False
        if hasattr(self, "_countdown_task") and not self._countdown_task.done():
            self.loop.call_soon_threadsafe(self._countdown_task.cancel)
        self.send_ts({"type": "disarm"})
        self.set_status("disarmed — opted out.")

    def action_pause(self):
        self.paused = not self.paused
        if not self.paused and getattr(self, "timeout_killed", False):
            self.set_status("resumed (server silent while paused — won't self-kill).")
        else:
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
        if hasattr(self, "_gather_task") and self.loop and self.connected:
            async def _shutdown():
                self.armed = False
                await self._send({"type": "disarm"})
                try:
                    await asyncio.wait_for(self.websocket.close(code=1000), timeout=1.5)
                except Exception:
                    pass
                self.loop.call_soon_threadsafe(self._gather_task.cancel)
            asyncio.run_coroutine_threadsafe(_shutdown(), self.loop)
        else:
            self.action_disarm()
            if hasattr(self, "_gather_task"):
                self.loop.call_soon_threadsafe(self._gather_task.cancel)

    def action_restart(self):
        self.needs_restart = True
        self.action_quit()
        self.set_status("Restarting and checking for updates...")

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
        if self.last_server_mode_safe:
            self.set_status("(safe mode) panic suppressed.")
            return
            
        self.set_status("PANIC pressed!")
        eid = uuid.uuid4().hex
        self.kill_events[eid] = time.monotonic()
        self.execute_kill("local panic")
        self.send_ts({"type": "panic", "event_id": eid})
        if self.mesh and self.mesh.transport:
            self.mesh.broadcast({
                "type": "peer_kill",
                "eid": eid,
                "cause": "panic",
                "reason": f"Panic triggered by {self.username}"
            })
        self.armed = False

    def _register_panic(self):
        try:
            keyboard.add_hotkey(self.panic_keybind, self.do_panic)
        except Exception as e:
            self.set_status(f"couldn't bind panic key: {e}")

    def _sync_check_game(self):
        try:
            if hasattr(self, "_game_pid"):
                try:
                    proc = psutil.Process(self._game_pid)
                    if proc.is_running() and proc.name().lower().startswith('gta5'):
                        return True
                except psutil.NoSuchProcess:
                    pass
                delattr(self, "_game_pid")
                
            for proc in psutil.process_iter(['name']):
                if proc.info['name'] and proc.info['name'].lower().startswith('gta5'):
                    self._game_pid = proc.pid
                    return True
        except Exception:
            pass
        return False

    async def game_status_loop(self):
        loop = asyncio.get_running_loop()
        while not self.stop.is_set():
            running = await loop.run_in_executor(None, self._sync_check_game)
            if running != self.game_running:
                self.game_running = running
                await self._send({"type": "game_status", "running": running})
            await asyncio.sleep(3.0)

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
            self.last_server_mode_safe = (self.server_mode == "safe")
            
            if self.mesh and self.mesh.transport:
                changed = False
                for c in self.roster:
                    uid = c.get("uuid")
                    if uid and uid != self.uuid and c.get("ip") and c.get("mesh_port"):
                        is_new = uid not in self.mesh.peers
                        self.mesh.upsert_peer(uid, c["ip"], c["mesh_port"], c.get("username", "Unknown"))
                        if is_new:
                            self.mesh.send_hello_all()
                            
                        # update config cache
                        if uid not in self.config["known_peers"]:
                            self.config["known_peers"][uid] = {"ip": c["ip"], "port": c["mesh_port"], "username": c.get("username", "Unknown")}
                            changed = True
                        elif self.config["known_peers"][uid].get("ip") != c["ip"] or self.config["known_peers"][uid].get("port") != c["mesh_port"]:
                            self.config["known_peers"][uid].update({"ip": c["ip"], "port": c["mesh_port"]})
                            changed = True
                if changed:
                    self.save()
        elif mtype == "ping":
            self.send_ts({"type": "pong", "t": data.get("t")})
        elif mtype == "kill":
            self.handle_kill(data.get("cause", "?"), data.get("reason", ""), event_id=data.get("event_id"))
        elif mtype == "kicked":
            self.armed = False
            self.suspended = True
            if hasattr(self, "_countdown_task") and not self._countdown_task.done():
                self._countdown_task.cancel()
            self.set_status(f"KICKED by server — {data.get('reason','')}. Press [A] to reconnect.")
        elif mtype == "settings_override":
            self.settings = normalize_settings(data.get("settings"))
            self.save()
            self.set_status("settings changed by server operator.")
        elif mtype == "countdown":
            if hasattr(self, "_countdown_task") and not self._countdown_task.done():
                self._countdown_task.cancel()
            self._countdown_task = self.loop.create_task(self._countdown(int(data.get("seconds", 5))))
        elif mtype == "scheduled_kill":
            if hasattr(self, "_countdown_task") and not self._countdown_task.done():
                self._countdown_task.cancel()
            self._countdown_task = self.loop.create_task(
                self._scheduled_countdown(data.get("delay_s", 5.0), data.get("seconds", 5))
            )
        elif mtype == "countdown_abort":
            if hasattr(self, "_countdown_task") and not self._countdown_task.done():
                self._countdown_task.cancel()
            self.set_status("server aborted countdown.")
        elif mtype == "notice":
            self.set_status(f"server: {data.get('msg','')}")
        elif mtype == "force_arm":
            self.armed = data.get("armed", False)
            if not self.armed:
                if hasattr(self, "_countdown_task") and not self._countdown_task.done():
                    self._countdown_task.cancel()
            self.set_status(f"Server forced you to {'ARM' if self.armed else 'DISARM'}.")
            if self.armed:
                self.timeout_killed = False
        elif mtype == "force_pause":
            self.paused = data.get("paused", False)
            if not self.paused and getattr(self, "timeout_killed", False):
                self.set_status("Server UNPAUSED you (server was silent — won't self-kill).")
            else:
                self.set_status(f"Server forced you to {'PAUSE' if self.paused else 'UNPAUSE'}.")
        elif mtype == "auth_failed":
            self.auth_failed_reason = data.get('reason','')
            self.stop.set()
        elif mtype == "restart":
            self.set_status("Server requested client restart. Restarting...")
            self.stop.set()
            self.needs_restart = True
            async def _restart_clean():
                self.armed = False
                await self._send({"type": "disarm"})
                try:
                    await asyncio.wait_for(self.websocket.close(code=1000), timeout=1.5)
                except Exception:
                    pass
                if hasattr(self, "_gather_task"):
                    self.loop.call_soon_threadsafe(self._gather_task.cancel)
            asyncio.run_coroutine_threadsafe(_restart_clean(), self.loop)

    async def _countdown(self, seconds):
        for n in range(seconds, 0, -1):
            if not self.armed:
                self.set_status("(disarmed) countdown ignored.")
                return
            if self.paused:
                self.set_status("(paused) countdown ignored.")
                return
            self.set_status(f"SYNCHRONIZED KILL IN {n}...")
            await asyncio.sleep(1)
        self.execute_kill("synchronized countdown")
        self.armed = False

    async def _scheduled_countdown(self, delay_s, total_secs):
        if not self.armed:
            self.set_status("(disarmed) countdown ignored.")
            return
        if self.paused:
            self.set_status("(paused) countdown ignored.")
            return
            
        end_time = time.monotonic() + delay_s
        while True:
            remaining = end_time - time.monotonic()
            if remaining <= 0:
                break
            self.set_status(f"SYNCHRONIZED KILL IN {max(1, int(remaining))}s (Latency adjusted)...")
            await asyncio.sleep(min(1.0, remaining))
            
        self.execute_kill("synchronized countdown")
        self.armed = False

    # ---- async loops -------------------------------------------------------
    async def connection_loop(self):
        uri = f"ws://{self.server_ip}:{self.port}"
        while not self.stop.is_set():
            if getattr(self, "suspended", False):
                await asyncio.sleep(1.0)
                continue
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
                        "mesh_port": self.mesh_port if self.mesh_enabled else 0
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
                body = {"type": "heartbeat"}
                if self.mesh:
                    body["mesh_seen"] = self.mesh.seen_uuids()
                await ws.send(_json(body))
            except Exception:
                break
            await asyncio.sleep(HEARTBEAT_INTERVAL)

    async def watchdog_loop(self):
        from common import ALONE_CONFIRM
        alone_since = None
        while not self.stop.is_set():
            await asyncio.sleep(0.5)
            now = time.monotonic()
            silent = now - self.last_server_msg
            server_ok = self.connected and silent <= SERVER_TIMEOUT
            alive = self.mesh.alive_peers() if self.mesh else []

            prev_mode = self.mode
            if server_ok:               self.mode = "CONNECTED"
            elif alive:                 self.mode = "MESH-ONLY"
            elif self.connected or self.armed:  self.mode = "ALONE"
            else:                       self.mode = "OFFLINE"
            
            if self.mode != prev_mode:
                if self.mode == "MESH-ONLY":
                    self.set_status(f"server lost — MESH-ONLY ({len(alive)} peers alive). Still in the run.")
                elif self.mode == "ALONE" and prev_mode == "MESH-ONLY":
                    self.set_status("lost server AND all peers — my own link is down.")
            if self.mode != "ALONE":
                alone_since = None

            if not self.armed:
                continue
            if self.paused:
                if silent > SERVER_TIMEOUT and not alive:
                    self.timeout_killed = True
                continue
            if self.mode == "CONNECTED":
                continue

            if self.mode == "MESH-ONLY":
                continue

            # ALONE while armed
            if alone_since is None:
                alone_since = now
                continue
            if not self.timeout_killed and (now - alone_since) >= ALONE_CONFIRM and silent > SERVER_TIMEOUT:
                self.timeout_killed = True
                self.execute_kill("lost server and all peers (own connection dead)")
                self.armed = False

    # ---- dashboard ---------------------------------------------------------
    def render(self):
        if self.mode == "CONNECTED":
            conn = Text("● CONNECTED", style="bold green")
        elif self.mode == "MESH-ONLY":
            conn = Text("◐ MESH-ONLY", style="bold yellow")
        elif self.mode == "ALONE":
            conn = Text("○ ALONE", style="bold red")
        else:
            conn = Text("○ offline", style="bold red")
            
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
        table.add_column("Ping", justify="right")
        table.add_column("Mesh", justify="center")
        table.add_column("Armed", justify="center")
        for _, label in SETTING_COLS:
            table.add_column(label, justify="center")
        if self.server_mode == "safe":
            table.add_column("Safe", justify="center")
        table.add_column("Game", justify="center")

        def mark(v):
            return Text("✓", style="green") if v else Text("·", style="dim")

        if self.mode == "MESH-ONLY":
            roster = [{
                "username": self.username, "connected": True, "armed": self.armed,
                "stale": False, "settings": self.settings, "uuid": self.uuid,
                "game_running": self.game_running, "ping_ms": -1
            }]
            if self.mesh:
                for p in self.mesh.alive_peers():
                    roster.append({
                        "username": p.username, "connected": True, "armed": p.armed,
                        "stale": False, "settings": {},
                        "uuid": p.uuid, "game_running": p.game_running, "ping_ms": -1,
                        "is_mesh_peer": True
                    })
        else:
            roster = self.roster or [{
                "username": self.username, "connected": self.connected,
                "armed": self.armed, "stale": False, "settings": self.settings,
                "uuid": self.uuid, "game_running": self.game_running,
            }]
            
        for c in roster:
            me = c.get("uuid") == self.uuid
            name = Text(c.get("username", "?") + (" (you)" if me else ""),
                        style="bold cyan" if me else "white")
            if c.get("is_mesh_peer"):
                cs = Text("mesh", style="yellow")
            elif not c.get("connected"):
                cs = Text("off", style="red")
            elif c.get("stale"):
                cs = Text("stale", style="yellow")
            else:
                cs = Text("on", style="green")
            
            ping_ms = c.get("ping_ms", -1)
            if ping_ms < 0 or not c.get("connected"):
                ping_txt = Text("—", style="dim")
            else:
                color = "green" if ping_ms < 100 else "yellow" if ping_ms < 250 else "red"
                ping_txt = Text(f"{int(ping_ms)}ms", style=color)
                
            mesh_status = Text("—", style="dim")
            if self.mesh:
                if me:
                    mesh_count = len(self.mesh.alive_peers())
                    if mesh_count > 0:
                        mesh_status = Text(f"{mesh_count}✓", style="green")
                else:
                    if c.get("uuid") in self.mesh.seen_uuids():
                        mesh_status = Text("✓", style="green")
                    else:
                        mesh_status = Text("·", style="dim")
                
            armed = Text("ARM", style="bold green") if c.get("armed") else Text("—", style="dim")
            if not c.get("connected"):
                game = Text("—", style="dim")
            else:
                game = Text("RUNNING", style="bold green") if c.get("game_running") else Text("off", style="dim")
            s = c.get("settings", {})
            row = [name, cs, ping_txt, mesh_status, armed] + [mark(s.get(key)) for key, _ in SETTING_COLS]
            if self.server_mode == "safe":
                row.append(Text("SAFE", style="yellow"))
            row.append(game)
            table.add_row(*row)

        s_hint = "  [S]Drop Safe Mode" if self.mode == "MESH-ONLY" and self.last_server_mode_safe else ""
        legend = ("[A]rm  [D]isarm  [P]ause  "
                  "[1]IgnPanic [2]Dry  "
                  f"[K] Rebind panic  [H]ide  [R]estart  [Q]uit{s_hint}")
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
            elif c == "s" and self.mode == "MESH-ONLY" and self.last_server_mode_safe:
                self.last_server_mode_safe = False
                self.set_status("Dropped cached safe mode. Panics will now work.")
            elif c == "r":
                self.action_restart()
                break
            elif c == "q":
                self.action_quit()
                break

    # ---- entry -------------------------------------------------------------
    async def run(self):
        self.loop = asyncio.get_running_loop()
        self.settings = normalize_settings(self.settings)
        
        from mesh import MeshTransport
        def get_heartbeat_body():
            return {
                "armed": self.armed,
                "game": self.game_running,
                "srv": self.connected and (time.monotonic() - self.last_server_msg) <= SERVER_TIMEOUT,
            }
        
        self.mesh = MeshTransport(
            self.uuid, self.username, self.password, self.mesh_port,
            self.on_mesh_kill, get_heartbeat_body
        )
        if self.mesh_enabled:
            await self.mesh.start(self.loop)
            for uid, p in self.config.get("known_peers", {}).items():
                if uid != self.uuid:
                    self.mesh.upsert_peer(uid, p.get("ip"), p.get("port"), p.get("username", "Unknown"))
            self.mesh.send_hello_all()
            
        async def firewall_hint_loop():
            if not self.mesh_enabled or not self.config.get("known_peers"):
                return
            await asyncio.sleep(10.0)
            if self.mesh and self.mesh.transport and not self.mesh.alive_peers() and not self.stop.is_set():
                self.set_status(f"Mesh: No peers heard. If blocked, run as admin: netsh advfirewall firewall add rule name=\"HeistSync Mesh\" dir=in action=allow protocol=UDP localport={self.mesh_port}")

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
            tasks = [
                self.connection_loop(), 
                self.watchdog_loop(), 
                self.game_status_loop(),
                self.tui_loop(),
                firewall_hint_loop()
            ]
            if self.mesh and self.mesh.transport:
                tasks.append(self.mesh.heartbeat_loop(self.stop))
                
            self._gather_task = asyncio.gather(*tasks)
            await self._gather_task
        except asyncio.CancelledError:
            pass
        finally:
            try:
                keyboard.remove_hotkey(self.panic_keybind)
                keyboard.remove_hotkey("ctrl+shift+h")
            except Exception:
                pass
            if self.mesh:
                self.mesh.stop()


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
        config["password"] = pw
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
    instance_port = config.get("instance_port", 48201)

    import socket
    instance_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        instance_socket.bind(('127.0.0.1', instance_port))
    except OSError:
        # Port is in use, another instance is running.
        instance_socket.sendto(b"UNHIDE", ('127.0.0.1', instance_port))
        sys.exit(99)

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
        elif getattr(client, "needs_restart", False):
            print("\nRestarting client as requested by server...")
            try:
                instance_socket.close()
            except Exception:
                pass
            print("Checking for updates...")
            import subprocess
            try:
                subprocess.call(["git", "pull"])
            except Exception as e:
                print(f"Update failed: {e}")
            os.execv(sys.executable, [sys.executable] + sys.argv)
        else:
            break
            
    print("\nExited. Good luck out there!")


if __name__ == "__main__":
    main()
