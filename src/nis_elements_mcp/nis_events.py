# nis_events.py
# ------------------------------------------------------------
# The NIS event log: one JSON line per thing done through the NIS bridge,
# appended to <NIS_LOG_DIR>/events-YYYY-MM-DD.jsonl (default
# ~/nis-mcp/logs). Whoever drives the bridge
# (the NIS MCP server, a script, the command line) writes here, and
# nis_monitor.py tails the file, so the monitor shows what
# Claude is doing from another process.
#
# One action is logged as a "start" line, then one closing line: "ok", or
# "failed" (the bridge refused it or NIS reported an error), "refused" (the
# client's e-stop check stopped it before anything was sent) or "error"
# (the bridge was not reachable). The start line is what lets the monitor
# say "nd_run has been running for 42 s" while the bridge is busy.
#
# Logging must never get in the way of the microscope, so a failed write
# is swallowed.
# ------------------------------------------------------------

from __future__ import annotations

import datetime
import json
from pathlib import Path

from nis_elements_mcp import settings

PHASES = ("start", "ok", "failed", "refused", "error")


def events_dir() -> Path:
    """NIS_LOG_DIR, else ~/nis-mcp/logs. Read on every call, so a change takes effect at once."""
    return settings.log_dir()


def events_path(day: datetime.date | None = None) -> Path:
    day = day or datetime.date.today()
    return events_dir() / f"events-{day.isoformat()}.jsonl"


def record(action: str, phase: str, **fields) -> dict:
    """Append one event and return it. Never raises."""
    event = {"ts": datetime.datetime.now().isoformat(timespec="milliseconds"),
             "action": action, "phase": phase, **fields}
    try:
        path = events_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return event


def read_from(path: Path, offset: int = 0) -> tuple[list[dict], int]:
    """Events appended to `path` after byte `offset`, and the new offset.
    A half-written last line is left for the next call; a missing file is
    an empty log."""
    try:
        with path.open("rb") as f:
            f.seek(offset)
            data = f.read()
    except OSError:
        return [], offset
    end = data.rfind(b"\n") + 1
    events = []
    for line in data[:end].splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events, offset + end


def summarize(event: dict) -> str:
    """One short line of detail for an event, for the monitor."""
    p, r = event.get("params") or {}, event.get("result") or {}
    action, phase = event.get("action"), event.get("phase")
    if phase == "start":
        return " ".join(f"{k}={v}" for k, v in p.items())
    if phase in ("refused", "error"):
        return str(event.get("message", ""))
    if phase == "failed":
        return str(r.get("error", r))
    took = f"  ({event['elapsed_s']} s)" if "elapsed_s" in event else ""
    if action == "move":
        return f"{_xy(r.get('before_um'))} -> {_xy(r.get('after_um'))}{took}"
    if action == "objective":
        return (f"{r.get('before')} -> {r.get('after')}, arrived={r.get('arrived')}"
                f", nosepiece {r.get('nosepiece_before')}->{r.get('nosepiece_after')}{took}")
    if action == "capture":
        return f"saved={r.get('saved')} {r.get('path', '')}{took}"
    if action == "nd_run":
        return f"nd_running={r.get('nd_running')}{took}"
    return took.strip()


def _xy(xy) -> str:
    try:
        return f"({xy[0]:.1f}, {xy[1]:.1f})"
    except (TypeError, IndexError, ValueError):
        return "?"
