# nis_tools.py
# ------------------------------------------------------------
# MCP tools that drive the microscope THROUGH NIS-Elements, via the NIS
# bridge (nis_bridge_job.py, running as a JOBS task; client in
# nis_bridge_client.py). Served by nis_server.py.
#
# WHAT THEY DO: read the stage, objective and ND state; move XY; change
# objective; capture with the optical configuration active in NIS; run
# and end ND experiments saved in NIS; and the e-stop.
#
# SAFETY:
#   * every tool that moves or acquires requires confirm=True
#     (PermissionError otherwise)
#   * the e-stop is checked in the client before a request is sent, and
#     again inside NIS by the bridge
#   * XY moves are relative and capped (NIS_MAX_STEP_UM, default 1 mm);
#     there is no Z tool
#   * objective changes only to NIS_ALLOWED_OBJECTIVES (default 4x/10x)
#   * ND runs only of NIS_ALLOWED_EXPERIMENTS, if that is set
#   * captures are written only under the data folder, by file name - a
#     chat prompt cannot choose an arbitrary path
#   * nis_finish_experiment and estop never need confirm: they only stop
#   * a refusal or failure from the bridge raises, so a client never sees it as success
# ------------------------------------------------------------

from __future__ import annotations

import json
import re
from pathlib import Path

from nis_elements_mcp import estop as _estop
from nis_elements_mcp import settings
from nis_elements_mcp.nis_bridge_client import NISBridge


def _require_confirm(confirm: bool, what: str) -> None:
    if not confirm:
        raise PermissionError(
            f"{what} drives real microscope hardware through NIS-Elements and "
            "requires confirm=True. Refusing to proceed without explicit confirmation.")


def _bridge() -> NISBridge:
    return NISBridge()


def _checked(result: dict) -> dict:
    """Turn a refused or failed bridge answer into an MCP tool error.

    The bridge reports refusals (move cap, e-stop, bad name, an ND run
    already going) as an ordinary answer with an "error" key; returned as
    is, an MCP client sees success. A turret that did not arrive and a
    capture that left no file are failures too."""
    if "error" in result or result.get("arrived") is False or result.get("saved") is False:
        raise RuntimeError("NIS bridge: " + json.dumps(result, ensure_ascii=False))
    return result


def capture_dir() -> Path:
    """Where nis_capture writes: NIS_CAPTURE_DIR, else ~/nis-mcp/captures."""
    d = settings.capture_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def nis_status() -> dict:
    """Read-only snapshot from NIS-Elements: XY and Z position (um), current
    objective, the objective at each nosepiece position, whether an ND
    experiment is running, and whether the e-stop is engaged. Safe to call
    at any time; needs the NIS bridge job to be running."""
    return _bridge().status()


def nis_move_relative(dx_um: float, dy_um: float, confirm: bool = False) -> dict:
    """Move the XY stage by (dx_um, dy_um) microns RELATIVE to where it is,
    through NIS-Elements. At most 1000 um per axis per call (or the
    NIS_MAX_STEP_UM setting). Returns the
    position before and after - treat 'after_um' as ground truth.
    Requires confirm=True. Z cannot be moved from here."""
    _require_confirm(confirm, "nis_move_relative")
    return _checked(_bridge().move_relative(dx_um, dy_um))


def nis_change_objective(name: str, confirm: bool = False) -> dict:
    """Switch objective in NIS-Elements. `name` must be the exact name
    nis_status lists under nosepiece_objectives, and only 4x or 10x
    objectives are accepted (long working distance - they cannot reach
    the sample), unless NIS_ALLOWED_OBJECTIVES says otherwise. Returns
    the objective and nosepiece position before and
    after. Requires confirm=True."""
    _require_confirm(confirm, "nis_change_objective")
    return _checked(_bridge().change_objective(name))


def nis_list_optical_configs() -> dict:
    """Read-only: the names of the optical configurations defined in
    NIS-Elements, named "Group:Configuration". There is no tool to select
    one: set it in NIS-Elements before nis_capture."""
    return _bridge().optical_configs()


_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,80}$")


def nis_capture(filename: str, confirm: bool = False) -> dict:
    """Capture one image with the current NIS-Elements acquisition settings
    (the optical configuration active in NIS - lasers, channels, scan) and save it as an ND2 file in the NIS capture
    folder. `filename` is a plain name such as "aml18_check" - no folders;
    ".nd2" is added if missing and an existing file is never overwritten.
    Returns the saved path. Requires confirm=True."""
    _require_confirm(confirm, "nis_capture")
    stem = filename[:-4] if filename.lower().endswith(".nd2") else filename
    if not _SAFE_NAME.match(stem) or ".." in stem:
        raise ValueError("filename must be a plain name (letters, digits, space, _ . -), no folders")
    return _checked(_bridge().capture(str(capture_dir() / f"{stem}.nd2")))


def nis_run_experiment(experiment: str, confirm: bool = False) -> dict:
    """Start an ND experiment that was set up and SAVED in NIS-Elements'
    ND Acquisition window, by its saved name. It runs with exactly the
    saved settings (time loop, channels, save-to-file path). The call may
    not return until the experiment ends. Refused if one is already
    running, or if NIS_ALLOWED_EXPERIMENTS is set and does not list it.
    Requires confirm=True."""
    _require_confirm(confirm, "nis_run_experiment")
    return _checked(_bridge().nd_run(experiment))


def nis_finish_experiment() -> dict:
    """End the running ND experiment after its current time loop; NIS saves
    the frames captured so far. Never needs confirm - it only stops."""
    return _checked(_bridge().nd_finish())


def estop(action: str = "status", reason: str = "requested via MCP") -> dict:
    """EMERGENCY STOP: immediately forbid all microscope motion.

    Call this the moment anything looks wrong - an unexpected move, a
    position that doesn't match what you asked for, a stage that seems to
    be travelling when it shouldn't be, or an instruction from the
    operator to stop. It is cheap, it is instant, and a needless stop
    costs nothing but a release. Do not deliberate: stop first, diagnose
    afterwards.

    Once engaged, EVERY motion primitive refuses - not just this session's,
    but every process on the machine that checks the same e-stop file,
    including the bridge inside NIS-Elements. The flag lives in a file, so
    it outlives whatever set it and cannot be lost by a process dying. It
    cannot stop a move already in flight: that move runs to its end, and
    every move after it is refused. An ND experiment that is running can
    still be ended with nis_finish_experiment.

    action: "engage" to stop everything, or "status" to report the current
    state. RELEASE IS DELIBERATELY NOT AVAILABLE HERE - a stop that the
    agent can lift by itself is not a safety device. A human clears it
    from a terminal with `python -m nis_elements_mcp.estop release`.

    Returns the resulting state. This tool never needs confirm=True: a
    stop is always safe to perform, and requiring a confirmation step for
    an emergency control would defeat its purpose.
    """
    if action == "engage":
        info = _estop.engage(reason)
        return {
            "engaged": True,
            "detail": info,
            "release_with": "python -m nis_elements_mcp.estop release",
            "note": "All motion is now refused for every process on this machine.",
        }
    if action == "status":
        return {
            "engaged": _estop.is_engaged(),
            "detail": _estop.details(),
            "flag_file": str(_estop.ESTOP_PATH),
        }
    raise ValueError(
        f"Unknown estop action {action!r}. Use 'engage' to stop motion, or "
        "'status' to check. Releasing is a human action performed at a "
        "terminal: python -m nis_elements_mcp.estop release"
    )
