# nis_monitor.py
# ------------------------------------------------------------
# Live terminal view of the NIS bridge. Run it in its own window:
#
#   python -m nis_elements_mcp.nis_monitor            # refresh every 1 s, Ctrl+C to leave
#   python -m nis_elements_mcp.nis_monitor --once     # print one snapshot and exit
#
# It shows the stage, objective, ND state and e-stop (polled from the bridge's
# /status) above a scrolling list of what was done through the bridge (the NIS
# event log, nis_events.py). It only reads: it never moves
# anything, and it does not log its own status polls.
#
# It also opens by itself in its own console window when the NIS MCP server
# starts (launch(), same pattern as the STOP panel) - you should not have to
# remember it. One window only: a named mutex stops a second copy.
# NIS_NO_MONITOR=1 turns the automatic launch off.
#
# While the bridge is busy with one long NIS call (an ND run or a capture
# blocks the bridge's single worker) /status cannot answer, so the monitor
# says so and names the action it is waiting on, from the event log.
# ------------------------------------------------------------

from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

from nis_elements_mcp import estop, nis_events, settings

ON_WINDOWS = sys.platform == "win32"
MONITOR_MUTEX = r"Local\nis-mcp-monitor"
STATUS_TIMEOUT_S = 2.0
EVENTS_KEPT = 200

RESET, DIM, RED, GREEN, YELLOW = "\x1b[0m", "\x1b[2m", "\x1b[31m", "\x1b[32m", "\x1b[33m"
PHASE_COLOUR = {"start": DIM, "ok": GREEN, "failed": RED, "refused": RED, "error": RED}


def monitor_running() -> bool:
    """True if a monitor window is already open (Windows only)."""
    if sys.platform != "win32":
        return False
    import ctypes
    k32 = ctypes.windll.kernel32
    h = k32.OpenMutexW(0x00100000, False, MONITOR_MUTEX)  # SYNCHRONIZE
    if h:
        k32.CloseHandle(h)
        return True
    return False


def launch() -> None:
    """Open the monitor in a new console window unless one is already open.

    Called when the NIS MCP server starts. Detached, so the window outlives
    the server; failures go to stderr (stdout is the MCP JSON-RPC channel)
    and never stop the caller.
    """
    if settings.monitor_disabled() or not ON_WINDOWS:
        return
    try:
        if monitor_running():
            return
        subprocess.Popen([sys.executable, "-m", "nis_elements_mcp.nis_monitor"],
                         stdin=subprocess.DEVNULL, stdout=None, stderr=None,
                         creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
    except Exception as exc:
        print(f"[nis_monitor] could not open the monitor window ({exc}). "
              f"Start it with: python -m nis_elements_mcp.nis_monitor", file=sys.stderr)


def _hold_mutex():
    """Take the single-instance mutex for this process's life; None if
    another monitor already holds it. Windows only."""
    if sys.platform != "win32":
        return True
    import ctypes
    h = ctypes.windll.kernel32.CreateMutexW(None, False, MONITOR_MUTEX)
    if ctypes.windll.kernel32.GetLastError() == 183:      # ERROR_ALREADY_EXISTS
        return None
    return h


def fetch_status(url: str) -> tuple[str, dict | None]:
    """("up", status) / ("busy", None) when the bridge accepts the connection
    but does not answer in time / ("down", None) when nothing is listening."""
    try:
        with urllib.request.urlopen(url + "/status", timeout=STATUS_TIMEOUT_S) as r:
            return "up", json.loads(r.read())
    except TimeoutError:
        # urllib lets only a timeout while *reading* the answer through bare:
        # the connection was made and NIS did not answer. (A connect that
        # times out - Windows retries a closed port for seconds - arrives
        # wrapped in URLError, below.)
        return "busy", None
    except (OSError, ValueError):
        return "down", None


def pending(events: list[dict]) -> dict | None:
    """The latest action that has a "start" line and no closing line yet."""
    open_starts: dict[str, dict] = {}
    for e in events:
        if e.get("phase") == "start":
            open_starts[e["action"]] = e
        else:
            open_starts.pop(e["action"], None)
    return max(open_starts.values(), key=lambda e: e["ts"], default=None)


def _short_objective(name: str | None) -> str:
    return (name or "?").replace("PLAN APO ", "").replace("λD ", "")


def render(link: str, status: dict | None, events: list[dict], estop_text: str,
           now: datetime.datetime, width: int = 100, rows: int = 20,
           colour: bool = True) -> str:
    c = (lambda code, s: f"{code}{s}{RESET}") if colour else (lambda code, s: s)
    lines = [c(DIM, "NIS monitor  ") + now.strftime("%Y-%m-%d %H:%M:%S")]
    bridge = {"up": c(GREEN, "UP"), "down": c(RED, "DOWN (job not running in NIS?)"),
              "busy": c(YELLOW, "BUSY (NIS is inside one long call)")}[link]
    estop_part = c(RED, estop_text) if estop.is_engaged() else c(GREEN, estop_text)
    lines.append(f"bridge: {bridge}    e-stop: {estop_part}")
    if status:
        x, y = status.get("xy_um", [float("nan")] * 2)
        lines.append(f"XY {x:.1f}, {y:.1f} um   Z {status.get('z_um', float('nan')):.2f} um   "
                     f"objective {_short_objective(status.get('current_objective'))} "
                     f"[nosepiece {status.get('nosepiece_position')}]   "
                     f"ND run: {'RUNNING' if status.get('nd_running') else 'idle'}")
    wait = pending(events)
    if wait:
        since = datetime.datetime.fromisoformat(wait["ts"])
        secs = max(0, int((now - since).total_seconds()))
        detail = nis_events.summarize(wait)
        lines.append(c(YELLOW, f"now: {wait['action']} {detail} - {secs} s so far"))
    lines.append(c(DIM, "-" * min(width, 100)))
    for e in events[-rows:]:
        phase = e.get("phase", "?")
        stamp = e.get("ts", "")[11:19]
        text = f"{stamp}  {e.get('action', '?'):<10} {c(PHASE_COLOUR.get(phase, ''), f'{phase:<7}')} "
        body = nis_events.summarize(e)
        room = max(10, width - len(stamp) - 22)
        lines.append(text + (body if len(body) <= room else body[:room - 1] + "…"))
    if not events:
        lines.append(c(DIM, "(no NIS actions logged today yet)"))
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="Live terminal view of the NIS bridge.")
    ap.add_argument("--url", default=settings.bridge_url())
    ap.add_argument("--interval", type=float, default=1.0, help="seconds between refreshes")
    ap.add_argument("--once", action="store_true", help="print one snapshot and exit")
    a = ap.parse_args()
    held = None if a.once else _hold_mutex()      # kept for the life of the process
    if not a.once and held is None:
        raise SystemExit("A NIS monitor is already open.")
    url = a.url.rstrip("/")

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    os.system("")                                   # lets a Windows console read ANSI codes
    events: list[dict] = []
    path, offset = nis_events.events_path(), 0
    try:
        while True:
            today = nis_events.events_path()
            if today != path:                       # past midnight: start the new day's file
                path, offset = today, 0
            new, offset = nis_events.read_from(path, offset)
            events = (events + new)[-EVENTS_KEPT:]
            link, status = fetch_status(url)
            estop_text = "ENGAGED" if estop.is_engaged() else "clear"
            size = shutil.get_terminal_size((100, 30))
            frame = render(link, status, events, estop_text, datetime.datetime.now(),
                           width=size.columns, rows=max(5, size.lines - 7),
                           colour=sys.stdout.isatty())
            if a.once:
                print(frame)
                return
            sys.stdout.write("\x1b[H\x1b[2J" + frame + "\n")
            sys.stdout.flush()
            time.sleep(a.interval)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
