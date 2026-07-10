# GTA Heist Sync

A small coordination tool for a group (usually 4) playing GTA Online together over
a private network (e.g. ZeroTier). Everyone "arms" the tool; when someone hits panic,
drops unexpectedly, or the server dies, everyone's GTA5 is closed at the same moment.
The point is to avoid getting left behind in a bad state during a synchronized action.

This is a rewrite of the original three-file version, adding server control, a live
dashboard, per-client settings that persist, and a lot of quality-of-life polish.

---

## What's here

| File | Purpose |
|------|---------|
| `server.py` | The coordinator + live CLI console. One person runs this. |
| `client.py` | Everyone (including the host) runs this. Live dashboard + menu. |
| `common.py` | Shared protocol, defaults, config helpers. |
| `launch_server.bat` / `launch_client.bat` | One-click launchers (client auto-elevates). |

Config files (`client_config.json`, `server_config.json`) are created on first run and
are **git-ignored** because they hold your password and identity. Copy the
`*.example.json` files if you want a starting point.

---

## Setup

1. Install Python 3.9+ and the dependencies:
   ```
   pip install -r requirements.txt
   ```
2. Everyone joins the same ZeroTier (or LAN) network.
3. Pick one machine as the **server**. Note its ZeroTier IP.
4. Set the **same `password`** on the server and every client (first-run prompt, or edit the config).

## Running

- **Host:** double-click `launch_server.bat`. You get a **live dashboard** of every
  connected player and their toggles, with an event log scrolling above it. Press
  **Enter** to drop into a command prompt (type `help`), run a command, then it returns
  to the dashboard.
- **Players:** double-click `launch_client.bat` (it asks for Administrator so the panic
  hotkey works while GTA is focused). Enter your username, the server IP, and the password
  on first run.

The client **never auto-exits.** A kill just drops you back to an idle state — press
**A** to re-arm and rejoin without relaunching anything.

---

## Client controls (single key press)

| Key | Action |
|-----|--------|
| `A` | Arm — join the run |
| `D` | Disarm — opt out |
| `P` | Pause — ignore all incoming kills (reversible) |
| `1` | Toggle *disconnect_kill* (my drop kills everyone) |
| `2` | Toggle *ignore_disconnect_kills* |
| `3` | Toggle *ignore_server_timeout_kills* |
| `4` | Toggle *ignore_other_panic* |
| `5` | Toggle *dry_run* (test mode — log instead of killing) |
| `K` | Rebind the panic key — just press the combo you want |
| `Q` | Quit |
| *panic key* | Kill your own game **and** everyone armed (default `Ctrl+Shift+F12`) |

The dashboard shows every connected player, their connection health, whether they're
armed, and each of their toggles (✓ = on). Your row is marked **(you)**.

## Server console commands

The server shows a live dashboard; press **Enter** to type any of these, then it
returns to the dashboard.

| Command | Action |
|---------|--------|
| `list` | Show the full roster + everyone's toggles + health |
| `safe [on\|off]` | Suppress **all** kills (global pause). Toggles if no arg. |
| `kill <user>` | Targeted kill of one player only — nobody else is affected |
| `kick <user>` | Remove someone from the session; their game keeps running, they can re-arm |
| `set <user\|all> <setting> on\|off` | Override a client's setting(s), no confirmation needed |
| `countdown [sec]` | Synchronized 3-2-1 kill for all armed players |
| `help` | List commands |

`<user>` is a username or the start of a uuid. `Ctrl+C` in the server window stops it.

---

## How kills are decided

- **Your own panic key** and a **server targeted kill** always fire (unless `dry_run`).
- **Disconnect kills** fire only if the dropping player has `disconnect_kill` on, after a
  short grace window (default 1s) so a brief network blip doesn't nuke everyone. Recipients
  can opt out with `ignore_disconnect_kills`.
- **Another player's panic** hits everyone armed except those with `ignore_other_panic`.
- **Server going silent** self-kills each armed client unless they have
  `ignore_server_timeout_kills`.
- **Safe mode** on the server suppresses every server-initiated kill.
- **Pause** on a client ignores everything incoming until resumed.

The **server is authoritative** over settings: an operator can change any client's toggles
(or everyone's at once) and the change is pushed down instantly and saved to that client's
config, so it sticks across restarts.

---

## Notes

- Settings and identity persist between runs. You configure once.
- Windows is the primary target (GTA5 + `taskkill` + global hotkeys). The client runs on
  other platforms for testing but the kill/hotkey paths are Windows-focused.
- Use `dry_run` while setting up so you can test the whole mesh without closing your game.
