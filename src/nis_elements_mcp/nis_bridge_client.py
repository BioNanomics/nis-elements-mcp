# nis_bridge.py
# ------------------------------------------------------------
# Client for the NIS bridge (nis_bridge_job.py), the
# JOBS task that exposes NIS-Elements macro calls on 127.0.0.1:8766 while
# it runs. Used by the NIS MCP server; also a command line for testing by
# hand:
#
#   python -m nis_elements_mcp.nis_bridge_client status
#   python -m nis_elements_mcp.nis_bridge_client move 100 0
#   python -m nis_elements_mcp.nis_bridge_client objective "Plan Fluor 10x Ph1 DLL"
#   python -m nis_elements_mcp.nis_bridge_client optconfs
#   python -m nis_elements_mcp.nis_bridge_client capture D:\...\check.nd2
#   python -m nis_elements_mcp.nis_bridge_client nd-run "C. elegans 3h"
#   python -m nis_elements_mcp.nis_bridge_client nd-finish
#   python -m nis_elements_mcp.nis_bridge_client shutdown
#
# The e-stop is checked HERE as well as in the bridge: a refused request
# never leaves this process, so a stopped microscope does not depend on
# the NIS side having been updated. Stopping an ND run (nd-finish) and
# shutting the bridge down are always allowed - they only end things.
#
# Every action here is also written to the NIS event log
# (nis_events.py), which `python -m nis_elements_mcp.nis_monitor`
# shows live. Status reads are not logged (the monitor polls them).
#
# NIS_BRIDGE_HOST / NIS_BRIDGE_PORT set the address (default
# http://127.0.0.1:8766). The XY step limit, the objective allow-list and
# the ND experiment allow-list (settings.py) are checked here before
# anything is sent, and the bridge checks its own copy of the first two.
# ------------------------------------------------------------

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

from nis_elements_mcp import estop, nis_events, settings
from nis_elements_mcp.nis_bridge_job import magnification



class BridgeUnavailable(RuntimeError):
    """The bridge job is not running (or not reachable)."""


class NISBridge:
    def __init__(self, url: str | None = None, timeout_s: float = 30.0):
        self.url = (url or settings.bridge_url()).rstrip("/")
        self.timeout_s = timeout_s

    def _call(self, method: str, path: str, body: dict | None = None,
              timeout_s: float | None = None) -> dict:
        data = json.dumps(body or {}).encode() if method == "POST" else None
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout_s or self.timeout_s) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                return {"http_status": e.code, **json.loads(e.read())}
            except ValueError:
                return {"http_status": e.code, "error": str(e)}
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            raise BridgeUnavailable(
                f"NIS bridge not reachable at {self.url} ({e}). Is the bridge job "
                "running in NIS-Elements? See docs/nis-bridge.md.") from e

    # Read-only
    def status(self) -> dict:
        return self._call("GET", "/status")

    def _act(self, action: str, params: dict, send, guarded: bool = True) -> dict:
        """Run one action and write it to the NIS event log (nis_events.py):
        a "start" line, then "ok", "failed", "refused" or "error"."""
        nis_events.record(action, "start", params=params)
        t0 = time.time()
        try:
            if guarded:
                estop.check()
            result = send()
        except estop.EStopEngaged as e:
            nis_events.record(action, "refused", params=params, message=str(e))
            raise
        except BridgeUnavailable as e:
            nis_events.record(action, "error", params=params, message=str(e))
            raise
        phase = "failed" if "error" in result else "ok"
        nis_events.record(action, phase, params=params, result=result,
                          elapsed_s=round(time.time() - t0, 1))
        return result

    def optical_configs(self) -> dict:
        return self._call("GET", "/optconfs")

    # Motion / acquisition: e-stop checked before anything is sent
    def move_relative(self, dx_um: float, dy_um: float) -> dict:
        return self._act("move", {"dx": dx_um, "dy": dy_um},
                         lambda: _step_refusal(dx_um, dy_um)
                         or self._call("POST", "/move", {"dx": dx_um, "dy": dy_um}))

    def change_objective(self, name: str) -> dict:
        return self._act("objective", {"name": name},
                         lambda: _objective_refusal(name)
                         or self._call("POST", "/objective", {"name": name}))

    def capture(self, path: str) -> dict:
        return self._act("capture", {"path": path},
                         lambda: self._call("POST", "/capture", {"path": path}, timeout_s=300))

    def nd_run(self, experiment: str, timeout_s: float = 7200.0) -> dict:
        # ND_RunExperiment may block until the run ends (unverified), so
        # the answer can take as long as the run itself.
        return self._act("nd_run", {"experiment": experiment},
                         lambda: _experiment_refusal(experiment)
                         or self._call("POST", "/nd/run", {"experiment": experiment},
                                       timeout_s=timeout_s))

    # Always allowed: these only end things
    def nd_finish(self) -> dict:
        return self._act("nd_finish", {}, lambda: self._call("POST", "/nd/finish"), guarded=False)

    def shutdown(self) -> dict:
        return self._act("shutdown", {}, lambda: self._call("POST", "/shutdown"), guarded=False)


# The limits from settings.py, checked before a request leaves this process.
# Each returns the refusal, shaped like the bridge's own, or None.

def _step_refusal(dx_um: float, dy_um: float) -> dict | None:
    limit = settings.max_step_um()
    if max(abs(dx_um), abs(dy_um)) > limit:
        return {"error": f"step over {limit:g} um refused (NIS_MAX_STEP_UM)"}
    return None


def _objective_refusal(name: str) -> dict | None:
    allowed = settings.allowed_magnifications()
    if magnification(name) not in allowed:
        listed = "/".join(f"{m:g}x" for m in allowed)
        return {"error": f"only {listed} objectives are allowed (NIS_ALLOWED_OBJECTIVES), got {name!r}"}
    return None


def _experiment_refusal(experiment: str) -> dict | None:
    allowed = settings.allowed_experiments()
    if allowed is not None and experiment not in allowed:
        return {"error": f"ND experiment {experiment!r} is not in NIS_ALLOWED_EXPERIMENTS "
                         f"({', '.join(allowed)})"}
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description="Talk to the NIS bridge job by hand.")
    ap.add_argument("--url", help="bridge address (default from NIS_BRIDGE_HOST/PORT, else http://127.0.0.1:8766)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    m = sub.add_parser("move", help="relative XY move in um (max NIS_MAX_STEP_UM per axis, default 1000)")
    m.add_argument("dx", type=float); m.add_argument("dy", type=float)
    o = sub.add_parser("objective", help="exact name as listed by status (NIS_ALLOWED_OBJECTIVES, default 4x/10x)")
    o.add_argument("name")
    sub.add_parser("optconfs", help="list the optical configurations")
    c = sub.add_parser("capture", help="capture one image and save it as .nd2")
    c.add_argument("path")
    r = sub.add_parser("nd-run", help="load a saved ND experiment by name and run it")
    r.add_argument("experiment")
    sub.add_parser("nd-finish", help="end the running ND experiment after this loop")
    sub.add_parser("shutdown", help="stop the bridge job")
    a = ap.parse_args()

    b = NISBridge(a.url)
    try:
        out = {"status": lambda: b.status(),
               "move": lambda: b.move_relative(a.dx, a.dy),
               "objective": lambda: b.change_objective(a.name),
               "optconfs": lambda: b.optical_configs(),
               "capture": lambda: b.capture(a.path),
               "nd-run": lambda: b.nd_run(a.experiment),
               "nd-finish": lambda: b.nd_finish(),
               "shutdown": lambda: b.shutdown()}[a.cmd]()
    except (estop.EStopEngaged, BridgeUnavailable) as e:
        raise SystemExit(str(e))
    # Objective names contain a lambda; a cp1252 Windows console cannot print it.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(json.dumps(out, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
