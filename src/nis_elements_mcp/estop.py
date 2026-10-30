# estop.py
# ------------------------------------------------------------
# Emergency stop for every process that can move this microscope.
#
#   python -m nis_elements_mcp.estop engage        # STOP EVERYTHING, NOW
#   python -m nis_elements_mcp.estop status
#   python -m nis_elements_mcp.estop release       # deliberate, separate action
#
# WHY THIS EXISTS. On 2026-09-21 a mosaic raster with a bad focus plane
# began driving Z toward the sample. Stopping it took several minutes and
# three separate interventions, because every mechanism available was the
# wrong shape:
#
#   * stopping the background SHELL left its python CHILD running, still
#     issuing moves - the process list, not the task list, was the truth
#   * a tool call the operator REJECTED had already spawned its process,
#     which kept looping Z moves regardless of the rejection
#   * a "STOP file" checked once per mosaic tile is not a stop; between
#     checks it still issued dozens of moves
#   * the Ti2 SDK has NO abort/halt command (searched: only ZESCAPE, which
#     is a retract, not a stop), so there is nothing to "cancel" with
#
# The lesson is that an e-stop must sit BELOW whatever is misbehaving. So
# this is enforced inside nis_sdk's move primitives themselves: no caller,
# no matter how confused, can move an axis while the stop is engaged,
# because the check happens after the caller has already decided to move.
#
# WHY A FILE. It must work across processes that do not know about each
# other - that was the whole failure. A file is visible to every process,
# survives the death of whoever set it, needs no server or port, and can
# be set by a human from any shell (or by creating the file by hand) when
# the agent itself is the thing malfunctioning.
#
# WHY A FIXED PATH, NOT data_root(). data_root() resolves relative to the
# working directory, and an MCP server launched by a desktop client gets a
# different cwd than a terminal. Two processes would then disagree about
# where the e-stop lives, which is precisely the failure this must not
# have. ESTOP_PATH is absolute and identical for every process on the
# machine.
#
# FAIL SAFE: if the state cannot be determined (unreadable directory, odd
# filesystem error), check() treats the stop as ENGAGED. An e-stop that
# fails open is not a safety device. The cost of the opposite error is a
# refused move and a clear message, which is recoverable; the cost of
# failing open is a driven objective.
#
# WHAT IT DOES NOT DO: it cannot stop a move the controller has already
# accepted. A move in flight runs to its end; the stop takes effect at the
# next move. move_xy splits long moves into HOP_UM hops, so an XY move
# stops within one hop (measured: 0.54 s, <= 4 mm).
#
# WHY NO HALT. engage() used to also write the current position back as
# the target, to freeze an axis mid-travel. estop_inflight_test showed
# that is worse than nothing: position reads are cached for the whole move
# (XY and Z), so the "current" position is the START, and the write queues
# behind the move. A 1 mm Z move ran to its end, then the halt drove Z all
# the way back - a second move, issued by the stop. Never add it back.
# ------------------------------------------------------------

from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

from nis_elements_mcp import settings

#: Absolute and machine-wide. Never derive this from the working
#: directory - see the module header.
ESTOP_PATH: Path = settings.estop_file()      # NIS_ESTOP_FILE, else ~/.nis-mcp/ESTOP


#: Held by the running STOP panel for its whole life, so every process can
#: tell whether one is already on screen. Windows frees it if the panel dies.
PANEL_MUTEX = r"Local\nis-mcp-estop-panel"


class EStopEngaged(RuntimeError):
    """Raised by any motion primitive while the e-stop is engaged."""


def is_engaged() -> bool:
    """True if motion is currently forbidden.

    Any error resolving the state counts as engaged - see FAIL SAFE.
    """
    try:
        return ESTOP_PATH.exists()
    except OSError:
        return True


def details() -> dict | None:
    """Who engaged the stop, when, and why - or None if it is clear."""
    try:
        if not ESTOP_PATH.exists():
            return None
        return json.loads(ESTOP_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Present but unreadable still means engaged; say so without a reason.
        return {"reason": "(e-stop file present but unreadable)"}


def check() -> None:
    """Raise EStopEngaged if motion is forbidden. Called by every move."""
    if is_engaged():
        info = details() or {}
        raise EStopEngaged(
            "E-STOP ENGAGED - motion refused. "
            f"engaged_at={info.get('engaged_at', '?')} "
            f"by={info.get('by', '?')} reason={info.get('reason', '?')}. "
            f"Release with: python -m nis_elements_mcp.estop release  "
            f"(or delete {ESTOP_PATH})"
        )


def engage(reason: str = "manual") -> dict:
    """Forbid all motion immediately. Sets the flag and nothing else.

    It never commands the stage - see WHY NO HALT in the module header.
    """
    info = {
        "engaged_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "by": f"pid {os.getpid()}",
        "reason": reason,
    }
    ESTOP_PATH.parent.mkdir(parents=True, exist_ok=True)
    ESTOP_PATH.write_text(json.dumps(info, indent=2), encoding="utf-8")
    return info


def release() -> None:
    """Allow motion again. Deliberately a separate, explicit action."""
    try:
        ESTOP_PATH.unlink()
    except FileNotFoundError:
        pass



def panel_running() -> bool:
    """True if a STOP panel is already on screen (Windows only)."""
    if sys.platform != "win32":
        return False
    import ctypes
    k32 = ctypes.windll.kernel32
    h = k32.OpenMutexW(0x00100000, False, PANEL_MUTEX)  # SYNCHRONIZE
    if h:
        k32.CloseHandle(h)
        return True
    return False


def launch_panel() -> None:
    """Put the STOP panel on screen unless one is already there.

    Called when a process first connects to the real stage, so the button
    appears however a run was started - MCP server, mosaic, timelapse, a
    one-liner. The 2026-09-21 run was started from a terminal, where no
    panel existed; a safety control you have to remember to start is one
    you will not have.

    Detached, not a child: the panel must outlive this process. Failures
    go to stderr - never stdout, which is the MCP JSON-RPC channel - and
    never stop the caller. NIS_NO_ESTOP_PANEL=1 turns it off.
    """
    if settings.estop_panel_disabled():
        return
    try:
        if panel_running():
            return
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
        subprocess.Popen([sys.executable, "-m", "nis_elements_mcp.estop_panel"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=flags)
    except Exception as exc:
        print(f"[estop] could not start the STOP panel ({exc}). "
              f"Stop manually with: python -m nis_elements_mcp.estop engage", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser(description="Emergency stop for microscope motion.")
    ap.add_argument("action", choices=("engage", "release", "status"))
    ap.add_argument("--reason", default="manual")
    args = ap.parse_args()

    if args.action == "engage":
        info = engage(args.reason)
        print("E-STOP ENGAGED")
        for k, v in info.items():
            print(f"  {k}: {v}")
        print(f"\nflag file: {ESTOP_PATH}")
        print("All motion is now refused. Release with: python -m nis_elements_mcp.estop release")
    elif args.action == "release":
        release()
        print(f"e-stop released - motion permitted again ({ESTOP_PATH} removed)")
    else:
        if is_engaged():
            print("ENGAGED - motion is refused")
            for k, v in (details() or {}).items():
                print(f"  {k}: {v}")
            sys.exit(2)
        print(f"clear - motion permitted (no {ESTOP_PATH})")


if __name__ == "__main__":
    main()
