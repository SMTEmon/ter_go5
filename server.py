import asyncio
import json
import logging
import os
import sys
import threading
import time

import websockets
from rich.console import Console
from rich.live import Live
from rich.logging import RichHandler
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from common import (
    PROTOCOL_VERSION, SERVER_PING_INTERVAL, CLIENT_TIMEOUT,
    BOOL_SETTINGS, SETTINGS_HELP, normalize_settings, parse_bool,
    load_config, save_config, SERVER_DEFAULTS, make_getch,
)

if os.name == "nt":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

console = Console()
# RichHandler prints log lines *above* the live dashboard instead of clobbering it.
logging.basicConfig(
    level=logging.INFO, format="%(message)s", datefmt="%H:%M:%S",
    handlers=[RichHandler(console=console, show_path=False, markup=False)],
)
log = logging.getLogger("server")

CONFIG_FILE = "server_config.json"

# Dashboard columns, in display order.
SETTING_COLS = [
    ("disconnect_kill", "DiscKill"),
    ("ignore_disconnect_kills", "IgnDisc"),
    ("ignore_server_timeout_kills", "IgnSrv"),
    ("ignore_other_panic", "IgnPanic"),
    ("dry_run", "Dry"),
]


class ClientRecord:
    def __init__(self, uid, username, websocket, settings):
        self.uuid = uid
        self.username = username
        self.websocket = websocket
        self.settings = settings
        self.armed = False
        self.connected = True
        self.last_heartbeat = time.monotonic()
        self.kicked = False          # intentionally removed; suppress disconnect-kill
        self.address = websocket.remote_address[0] if websocket else "?"


class HeistServer:
    def __init__(self, config):
        self.config = config
        self.clients = {}            # uuid -> ClientRecord
        self.safe_mode = False       # True = suppress ALL server-initiated kills
        self.grace = float(config.get("grace_seconds", 1.0))
        self.loop = None

    # ---- persistence -------------------------------------------------------
    def remember(self, rec):
        self.config["saved_settings"][rec.uuid] = rec.settings
        self.config["saved_names"][rec.uuid] = rec.username
        save_config(CONFIG_FILE, self.config)

    # ---- lookup helpers ----------------------------------------------------
    def find(self, token):
        """Find a connected client by username (case-insensitive) or uuid prefix."""
        token = token.strip().lower()
        for rec in self.clients.values():
            if rec.username.lower() == token or rec.uuid.lower().startswith(token):
                return rec
        return None

    # ---- outbound ----------------------------------------------------------
    async def send(self, rec, message):
        try:
            await rec.websocket.send(json.dumps(message))
        except Exception:
            pass

    async def broadcast(self, message, only_armed=False, exclude=None):
        for rec in list(self.clients.values()):
            if not rec.connected:
                continue
            if only_armed and not rec.armed:
                continue
            if exclude and rec.uuid == exclude:
                continue
            await self.send(rec, message)

    def roster_payload(self):
        now = time.monotonic()
        clients = []
        for rec in self.clients.values():
            stale = rec.connected and (now - rec.last_heartbeat) > CLIENT_TIMEOUT
            clients.append({
                "uuid": rec.uuid,
                "username": rec.username,
                "armed": rec.armed,
                "connected": rec.connected,
                "stale": stale,
                "settings": rec.settings,
            })
        return {"type": "roster", "server_mode": "safe" if self.safe_mode else "normal",
                "grace": self.grace, "clients": clients}

    async def push_roster(self):
        await self.broadcast(self.roster_payload())

    # ---- kills -------------------------------------------------------------
    async def trigger_kill(self, cause, reason, exclude=None, target=None):
        """Broadcast a kill. Clients enforce their own ignore-toggles except
        for a targeted kill, which always lands."""
        if cause != "targeted" and self.safe_mode:
            log.warning(f"[SAFE MODE] Kill suppressed ({cause}): {reason}")
            return

        msg = {"type": "kill", "cause": cause, "reason": reason}
        if target:
            rec = self.clients.get(target)
            if rec and rec.connected:
                await self.send(rec, msg)
                log.warning(f"TARGETED KILL -> {rec.username}: {reason}")
            return

        log.warning(f"KILL broadcast ({cause}) to armed players: {reason}")
        await self.broadcast(msg, only_armed=True, exclude=exclude)

    async def schedule_disconnect_kill(self, rec):
        """Wait the grace window; if the armed player hasn't returned, kill."""
        uid, name = rec.uuid, rec.username
        await asyncio.sleep(self.grace)
        current = self.clients.get(uid)
        if current and current.connected:
            return  # they reconnected in time
        log.warning(f"Grace expired for {name}; triggering disconnect kill.")
        await self.trigger_kill("disconnect", f"{name} dropped and did not return")

    # ---- settings overrides ------------------------------------------------
    async def apply_override(self, rec, key, value):
        rec.settings[key] = value
        self.remember(rec)
        await self.send(rec, {"type": "settings_override", "settings": rec.settings})
        await self.push_roster()

    # ---- connection handling ----------------------------------------------
    async def handle_client(self, websocket):
        rec = None
        try:
            raw = await asyncio.wait_for(websocket.recv(), timeout=10)
            hello = json.loads(raw)
        except Exception:
            return

        if hello.get("type") != "hello" or hello.get("password") != self.config["password"]:
            await websocket.send(json.dumps({"type": "auth_failed", "reason": "Bad password or handshake"}))
            log.info(f"Rejected connection from {websocket.remote_address[0]} (auth).")
            return

        uid = hello.get("uuid") or "?"
        username = hello.get("username") or f"Player-{uid[:4]}"
        client_ver = hello.get("version", "?")
        if client_ver.split(".")[0] != PROTOCOL_VERSION.split(".")[0]:
            log.warning(f"{username} runs protocol {client_ver}, server is {PROTOCOL_VERSION}.")

        # Server-remembered settings win; otherwise take what the client offered.
        if uid in self.config["saved_settings"]:
            settings = normalize_settings(self.config["saved_settings"][uid])
        else:
            settings = normalize_settings(hello.get("settings"))

        existing = self.clients.get(uid)
        if existing:  # reconnect: reuse identity, keep armed state
            existing.websocket = websocket
            existing.connected = True
            existing.username = username
            existing.settings = settings
            existing.last_heartbeat = time.monotonic()
            existing.kicked = False
            rec = existing
            log.info(f"{username} reconnected ({rec.address}).")
        else:
            rec = ClientRecord(uid, username, websocket, settings)
            self.clients[uid] = rec
            log.info(f"{username} connected ({rec.address}).")

        self.remember(rec)
        await self.send(rec, {
            "type": "welcome", "your_uuid": uid, "grace": self.grace,
            "server_mode": "safe" if self.safe_mode else "normal",
            "settings": rec.settings, "version": PROTOCOL_VERSION,
        })
        await self.push_roster()

        try:
            async for raw in websocket:
                await self.handle_message(rec, json.loads(raw))
        except Exception:
            pass
        finally:
            await self.on_disconnect(rec)

    async def handle_message(self, rec, data):
        action = data.get("type")
        if action == "heartbeat":
            rec.last_heartbeat = time.monotonic()
        elif action == "arm":
            rec.armed = True
            log.info(f"{rec.username} ARMED. ({self.armed_count()} armed)")
            await self.push_roster()
        elif action == "disarm":
            rec.armed = False
            log.info(f"{rec.username} disarmed.")
            await self.push_roster()
        elif action == "panic":
            log.warning(f"PANIC from {rec.username}!")
            await self.trigger_kill("panic", f"Panic triggered by {rec.username}", exclude=rec.uuid)
        elif action == "settings_update":
            # Client changed its own settings locally; accept and remember.
            rec.settings = normalize_settings(data.get("settings"))
            self.remember(rec)
            await self.push_roster()

    async def on_disconnect(self, rec):
        rec.connected = False
        rec.last_heartbeat = time.monotonic()
        log.info(f"{rec.username} disconnected.")
        await self.push_roster()
        if rec.armed and rec.settings.get("disconnect_kill") and not rec.kicked and not self.safe_mode:
            log.warning(f"Armed {rec.username} dropped; grace timer ({self.grace}s) started.")
            self.loop.create_task(self.schedule_disconnect_kill(rec))

    def armed_count(self):
        return sum(1 for r in self.clients.values() if r.armed and r.connected)

    # ---- background loops --------------------------------------------------
    async def keepalive_loop(self):
        while True:
            await asyncio.sleep(SERVER_PING_INTERVAL)
            await self.broadcast({"type": "ping", "t": time.time()})

    async def stale_watch_loop(self):
        prev = {}
        while True:
            await asyncio.sleep(1.0)
            now = time.monotonic()
            changed = False
            for rec in self.clients.values():
                stale = rec.connected and (now - rec.last_heartbeat) > CLIENT_TIMEOUT
                if prev.get(rec.uuid) != stale:
                    prev[rec.uuid] = stale
                    changed = True
                    if stale:
                        log.warning(f"{rec.username} is not responding (stale heartbeat).")
            if changed:
                await self.push_roster()

    def dispatch_cli(self, line):
        parts = line.split()
        cmd = parts[0].lower()
        args = parts[1:]

        def schedule(coro):
            asyncio.run_coroutine_threadsafe(coro, self.loop)

        if cmd in ("help", "h", "?"):
            print_help()
        elif cmd in ("list", "ls", "status"):
            pass # Roster is always visible now
        elif cmd == "safe":
            mode = parse_bool(args[0]) if args else not self.safe_mode
            self.safe_mode = bool(mode)
            log.info(f"SAFE MODE {'ON — all kills suppressed' if self.safe_mode else 'OFF'}.")
            schedule(self.push_roster())
        elif cmd == "kill":
            if not args:
                log.info("usage: kill <user>")
                return
            rec = self.find(args[0])
            if not rec:
                log.info(f"no connected client matching '{args[0]}'.")
                return
            schedule(self.trigger_kill("targeted", "Server targeted kill", target=rec.uuid))
        elif cmd == "kick":
            if not args:
                log.info("usage: kick <user>")
                return
            self.do_kick(args[0], schedule)
        elif cmd == "countdown":
            secs = int(args[0]) if args and args[0].isdigit() else 5
            schedule(self.broadcast({"type": "countdown", "seconds": secs}, only_armed=True))
            log.info(f"Countdown ({secs}s) sent to armed players.")
        elif cmd == "set":
            self.do_set(args, schedule)
        elif cmd in ("quit", "exit"):
            log.info("Use Ctrl+C in this window to stop the server.")
        else:
            log.info(f"unknown command '{cmd}'. Type 'help'.")

    def do_kick(self, token, schedule):
        rec = self.find(token)
        if not rec:
            log.info(f"no connected client matching '{token}'.")
            return
        rec.kicked = True
        rec.armed = False

        async def _kick():
            await self.send(rec, {"type": "kicked", "reason": "Removed from session by server"})
            try:
                await rec.websocket.close()
            except Exception:
                pass
            await self.push_roster()
        schedule(_kick())
        log.info(f"Kicked {rec.username} (their game keeps running; they can re-arm).")

    def do_set(self, args, schedule):
        if len(args) < 3:
            log.info("usage: set <user|all> <setting> <on|off>")
            options = ", ".join(short for _, short in SETTING_COLS)
            log.info(f"settings: {options} (or full names)")
            return
        
        alias_map = {}
        for long, short in SETTING_COLS:
            alias_map[short.lower()] = long
            alias_map[long.lower()] = long

        who, key_input, valstr = args[0], args[1], args[2]
        key = alias_map.get(key_input.lower())
        
        if not key:
            options = ", ".join(short for _, short in SETTING_COLS)
            log.info(f"unknown setting '{key_input}'. Options: {options}")
            return
        val = parse_bool(valstr)
        if val is None:
            log.info(f"'{valstr}' is not on/off.")
            return
        if who.lower() == "all":
            targets = list(self.clients.values())
        else:
            rec = self.find(who)
            targets = [rec] if rec else []
        if not targets:
            log.info(f"no connected client matching '{who}'.")
            return
        for rec in targets:
            schedule(self.apply_override(rec, key, val))
        names = ", ".join(r.username for r in targets)
        log.info(f"set {key} = {'on' if val else 'off'} for: {names}")

    def print_roster(self):
        pass # Dashboard makes this obsolete


class ServerConsole:
    """Live dashboard + seamless command input, run in a background thread.
    
    Log lines stream above the dashboard (via RichHandler).
    """
    def __init__(self, server):
        self.server = server
        self.getch = make_getch()
        self.input_buffer = ""
        self.history = []
        self.history_idx = 0

    def get_suggestion(self):
        buf = self.input_buffer.lstrip()
        parts = buf.split()
        cmd = parts[0].lower() if parts else ""
        if not buf:
            return "Commands: safe, kill, kick, set, countdown, help"
        
        if "safe".startswith(cmd):
            return "safe [on|off] - suppress ALL kills"
        elif "kill".startswith(cmd):
            return "kill <user> - targeted kill of one player only"
        elif "kick".startswith(cmd):
            return "kick <user> - remove from session; game keeps running"
        elif "set".startswith(cmd):
            if len(parts) == 1 or (len(parts) == 2 and not buf.endswith(" ")):
                return "set <user|all> <setting> <on|off>"
            elif len(parts) == 2 or (len(parts) == 3 and not buf.endswith(" ")):
                return "set <user|all> [DiscKill | IgnDisc | IgnSrv | IgnPanic | Dry] <on|off>"
            else:
                return "set <user|all> <setting> [on|off]"
        elif "countdown".startswith(cmd):
            return "countdown [sec] - synchronized 3-2-1 kill"
        elif "help".startswith(cmd):
            return "help - show detailed help"
        return "Unknown command (type help for list)"

    def render(self):
        srv = self.server
        safe = srv.safe_mode
        clients = list(srv.clients.values())
        armed = sum(1 for r in clients if r.armed and r.connected)

        header = Table.grid(expand=True)
        header.add_column(justify="left")
        header.add_column(justify="right")
        header.add_row(
            Text.assemble(("Heist Server  ", "bold cyan"),
                          (f"{len(clients)} clients · {armed} armed", "white")),
            Text(f"mode: {'SAFE' if safe else 'normal'}   grace: {srv.grace}s",
                 style="bold yellow" if safe else "dim"),
        )

        table = Table(expand=True, header_style="bold")
        table.add_column("Player")
        table.add_column("IP", style="dim")
        table.add_column("Conn", justify="center")
        table.add_column("Armed", justify="center")
        for _, label in SETTING_COLS:
            table.add_column(label, justify="center")

        def mark(v):
            return Text("✓", style="green") if v else Text("·", style="dim")

        now = time.monotonic()
        if not clients:
            table.add_row("(waiting for players...)", "", "", "", *["" for _ in SETTING_COLS])
        for rec in clients:
            if rec.connected:
                stale = (now - rec.last_heartbeat) > CLIENT_TIMEOUT
                cs = Text("stale", style="yellow") if stale else Text("on", style="green")
            else:
                cs = Text("off", style="red")
            armedt = Text("ARM", style="bold green") if rec.armed else Text("—", style="dim")
            row = [Text(rec.username), Text(rec.address), cs, armedt]
            row += [mark(rec.settings.get(k)) for k, _ in SETTING_COLS]
            table.add_row(*row)

        legend = Text(self.get_suggestion(), style="yellow")
        prompt = Text(f"command> {self.input_buffer}█", style="bold cyan")
        
        body = Table.grid(expand=True)
        body.add_row(header)
        body.add_row(table)
        body.add_row(legend)
        body.add_row(prompt)
        return Panel(body, title="GTA Heist Sync — Server", border_style="cyan")

    def run(self):
        with Live(get_renderable=self.render, console=console, refresh_per_second=15, screen=False) as live:
            while True:
                ch = self.getch()
                if ch is not None:
                    if ch in ('\r', '\n'):
                        line = self.input_buffer.strip()
                        self.input_buffer = ""
                        if line:
                            if not self.history or self.history[-1] != line:
                                self.history.append(line)
                            self.history_idx = len(self.history)
                            self.server.dispatch_cli(line)
                    elif ch == '\x08': # backspace
                        self.input_buffer = self.input_buffer[:-1]
                    elif ch == "UP":
                        if self.history:
                            self.history_idx = max(0, self.history_idx - 1)
                            self.input_buffer = self.history[self.history_idx]
                    elif ch == "DOWN":
                        if self.history:
                            if self.history_idx < len(self.history) - 1:
                                self.history_idx += 1
                                self.input_buffer = self.history[self.history_idx]
                            else:
                                self.history_idx = len(self.history)
                                self.input_buffer = ""
                    else:
                        # only printable characters
                        if len(ch) == 1 and (ch.isprintable() or ch == ' '):
                            self.input_buffer += ch
                else:
                    time.sleep(0.05)


def print_help():
    print("""
  === GTA Heist Sync — server console ===
  list                          show every client + their toggles + health
  safe [on|off]                 suppress ALL kills (toggle if no arg)
  kill <user>                   targeted kill of one player only
  kick <user>                   remove from session; game keeps running
  set <user|all> <setting> on|off   override a client's setting(s)
  countdown [sec]               synchronized 3-2-1 kill for armed players
  help                          this help
  (Ctrl+C in this window stops the server.)
  settings: """ + ", ".join(f"{k}" for k in SETTINGS_HELP) + "\n")


async def main():
    config = load_config(CONFIG_FILE, SERVER_DEFAULTS)
    server = HeistServer(config)
    server.loop = asyncio.get_running_loop()

    threading.Thread(target=ServerConsole(server).run, daemon=True).start()
    server.loop.create_task(server.keepalive_loop())
    server.loop.create_task(server.stale_watch_loop())

    log.info(f"Heist server listening on {config['host']}:{config['port']} (protocol {PROTOCOL_VERSION}).")
    if config["password"] == "changeme":
        log.warning("Password is still 'changeme' — edit server_config.json and share it with players.")

    async with websockets.serve(server.handle_client, config["host"], config["port"]):
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nServer stopped.")
