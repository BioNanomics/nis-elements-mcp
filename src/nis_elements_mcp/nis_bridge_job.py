# IMPORTANT: 'limjob' must be imported like this (not from nor as)
try:
    import limjob
except ImportError:          # off the microscope (tests): the module is only needed for type hints
    limjob = None

# bridge_job.py
# ------------------------------------------------------------
# The NIS-Elements side of the NIS bridge: paste this file into a JOBS
# Python task and run the job. While the job runs, NIS answers a small
# JSON-over-HTTP API on 127.0.0.1:8766, which nis_bridge_client.py (and
# through it the NIS MCP server) talks to. This file must stay standalone:
# NIS's own Python has only the standard library, so nothing here may
# import from nis_elements_mcp.
#
#   GET  /status                      positions, objective, ND run state
#   POST /move        {"dx", "dy"}    relative XY move, um
#   POST /objective   {"name"}        Stg_SetNosepiecePosition (turns the turret) then ChangeObjective
#                                     (NIS calibration), allow-listed names only
#   GET  /optconfs                    names of the optical configurations (read-only)
#   POST /capture     {"path"}        Capture, save as ND2, close the document
#   POST /nd/run      {"experiment"}  ND_LoadExperiment + ND_RunExperiment(0)
#   POST /nd/finish                   ND_FinishExperiment (stop after this loop)
#   POST /shutdown                    stop serving and end the job
#
# STOPPING THE JOB. Do NOT use the JOBS Abort button: a Python task's
# RunContext (limjob.py) has no abort flag, so Abort cannot interrupt the
# serving loop and NIS freezes waiting for it (seen on the scope
# 2026-10-14). Stop it with
#   python -m nis_elements_mcp.nis_bridge_client shutdown
# As a backstop the job also ends itself after IDLE_STOP_S with no request.
#
# WHY A BRIDGE AND NOT THE MCP SERVER ITSELF INSIDE NIS. Nothing has to be
# installed into NIS's own Python (Nikon pins its packages), a crash in our
# code cannot take NIS down with it, and the MCP server keeps the e-stop,
# confirm gate and tests it already has. This file only translates HTTP
# requests into NIS macro calls.
#
# HOW NIS IS CALLED. NIS macro functions are exported by g5_regprocs.dll
# and called through ctypes - the way NIS's own limpy.macro calls
# WaitText. Signatures are from the NIS 6.20 macro reference
# (Docs/nis/eng_ar on the microscope PC). A macro char* is wchar_t* in
# this build (limpy passes WaitText's text as c_wchar_p).
#
# THREADING. A JOBS Python task runs on NIS's main thread, and serving there
# froze NIS (2026-10-15). So run() starts serve() on a worker thread and
# returns. NIS is not known to be thread-safe, so every NIS call still runs
# on that one worker thread; the HTTP threads only queue requests and wait
# for the answer. Whether macro calls (Ti2 moves, Capture) are allowed off
# the main thread is UNVERIFIED. A call that takes long (ND_RunExperiment
# may block until the run ends - unverified) delays the requests queued
# behind it.
#
# SAFETY, enforced here as well as in the client:
#   * motion and acquisition only on POST, so a browser cannot trigger them
#   * nothing moves or starts while the e-stop file exists (the same file
#     estop.py uses)
#   * XY moves are relative and capped at MAX_STEP_UM; Z is never moved
#   * objective changes only to ALLOWED_MAGNIFICATIONS (default 4x/10x,
#     whose working distances of 20 and 4 mm cannot reach the dish)
#   * serves on 127.0.0.1 only
# ------------------------------------------------------------
import ctypes as ct
import http.server
import json
import os
import queue
import re
import threading
import time
from pathlib import Path

# Settings. Inside NIS they come from NIS's own environment, which is
# usually empty, so the defaults apply. To change one, set the variable
# (NIS_BRIDGE_PORT, NIS_MAX_STEP_UM, NIS_ALLOWED_OBJECTIVES, NIS_ESTOP_FILE)
# before starting NIS, or edit the default here before pasting the file.
# The client side (settings.py) uses the same names and defaults.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8766             # 8765 is the plain port probe (nis_port_probe.py)
DEFAULT_MAX_STEP_UM = 1000.0
DEFAULT_ALLOWED_OBJECTIVES = "4,10"    # long working distances: they cannot reach the dish
VERSION = 1
UI_TICK_MS = 50                 # how often NIS's main thread looks for queued UI calls
UI_CALL_TIMEOUT_S = 600.0       # give up waiting for the main thread after this long
# "4x" / "10x" as a whole magnification - not the 4x inside "14x" or "40x"
_MAG = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*[xX](?![a-zA-Z0-9])")


def parse_magnifications(text: str) -> "tuple[float, ...]":
    """'4,10' or '4x, 10x' -> (4.0, 10.0)."""
    return tuple(float(p.strip().rstrip("xX")) for p in text.split(",") if p.strip())


def magnification(name: str) -> "float | None":
    """The one magnification in an objective name ('PLAN APO 10x' -> 10.0), else None."""
    mags = {float(m) for m in _MAG.findall(name)}
    return mags.pop() if len(mags) == 1 else None


def estop_file() -> Path:
    """NIS_ESTOP_FILE, else ~/.nis-mcp/ESTOP: absolute, the same for every process."""
    return Path(os.environ.get("NIS_ESTOP_FILE", "").strip() or Path.home() / ".nis-mcp" / "ESTOP")


PORT = int(os.environ.get("NIS_BRIDGE_PORT", "").strip() or DEFAULT_PORT)
MAX_STEP_UM = float(os.environ.get("NIS_MAX_STEP_UM", "").strip() or DEFAULT_MAX_STEP_UM)
ALLOWED_MAGNIFICATIONS = parse_magnifications(os.environ.get("NIS_ALLOWED_OBJECTIVES", "").strip()
                                              or DEFAULT_ALLOWED_OBJECTIVES)
ESTOP = estop_file()

IDLE_STOP_S = 4 * 3600.0        # end the job if nothing has asked for this long
TURRET_SETTLE_S = 25.0          # how long to wait for the nosepiece to arrive
TURRET_POLL_S = 0.25
MOVE_RELATIVE = 1               # StgMoveXY 'relative' argument
ND2_ALL_LAYERS = 14             # ImageSaveAs ImType: all layers (ND2 file)
QUERYSAVE_NO = 2                # CloseCurrentDocument: close without asking


class Macro:
    """ctypes view of the NIS macro functions this bridge uses."""

    SIGNATURES = {
        "StgGetPosXY": [ct.POINTER(ct.c_double), ct.POINTER(ct.c_double)],
        "StgGetPosZ": [ct.POINTER(ct.c_double), ct.c_int],
        "StgMoveXY": [ct.c_double, ct.c_double, ct.c_int],
        "Stg_GetNosepiecePosition": [],
        "Stg_GetNosepiecePositions": [],
        "Stg_GetNosepieceObjectiveName": [ct.c_int, ct.c_wchar_p, ct.c_int],
        "GetCurrentObjName": [ct.c_wchar_p],
        "Stg_SetNosepiecePosition": [ct.c_int],
        "ChangeObjective": [ct.c_wchar_p],
        "GetOptConfCount": [],
        "GetOptConfName": [ct.c_int, ct.c_wchar_p, ct.c_int],
        "Capture": [],
        "ImageSaveAs": [ct.c_wchar_p, ct.c_int, ct.c_int],
        "CloseCurrentDocument": [ct.c_int],
        "ND_LoadExperiment": [ct.c_wchar_p],
        "ND_RunExperiment": [ct.c_int],
        "ND_IsInExperimentCapture": [],
        "ND_FinishExperiment": [],
    }

    def __init__(self, dll=None):
        self.dll = dll if dll is not None else ct.cdll.g5_regprocs
        for name, args in self.SIGNATURES.items():
            fn = getattr(self.dll, name)
            fn.argtypes, fn.restype = args, ct.c_int

    def __getattr__(self, name):
        return getattr(self.dll, name)


def _estop_engaged() -> bool:
    try:
        return ESTOP.exists()
    except OSError:
        return True             # cannot tell -> treat as engaged, like estop.py


class MainThread:
    """Runs calls on NIS's main (UI) thread.

    Capture, ImageSaveAs, CloseCurrentDocument and the ND calls open or close
    NIS windows. Called from the worker thread they crashed nis_ar.exe
    (Qt5Core.dll, 0xc0000409, 2026-10-16). run() is the one place our code
    is on the main thread, so it calls install(), which registers a Win32
    thread timer there. NIS's own message loop then calls _tick on the main
    thread every UI_TICK_MS, and _tick runs what the worker queued with
    call(). If install() was never called, call() refuses.
    """

    def __init__(self, tick_ms: int = UI_TICK_MS):
        self.tick_ms = tick_ms
        self.installed = False
        self._jobs = queue.Queue()
        self._busy = False              # a call is running: do not re-enter from a nested message loop
        self._timer_proc = None         # keep the ctypes callback alive
        self.main_thread_id = None

    def install(self) -> None:
        """Call from the main thread only (JOBS run())."""
        from ctypes import wintypes
        user32 = ct.windll.user32
        proc_type = ct.WINFUNCTYPE(None, wintypes.HWND, wintypes.UINT, ct.c_size_t, wintypes.DWORD)
        self._timer_proc = proc_type(lambda hwnd, msg, tid, tick: self._tick())
        user32.SetTimer.argtypes = [wintypes.HWND, ct.c_size_t, wintypes.UINT, proc_type]
        user32.SetTimer.restype = ct.c_size_t
        if not user32.SetTimer(None, 0, self.tick_ms, self._timer_proc):
            raise OSError("SetTimer failed")
        self.main_thread_id = threading.get_ident()
        self.installed = True

    def _tick(self) -> None:
        if self._busy:
            return
        self._busy = True
        try:
            while True:
                try:
                    fn, box, cancelled = self._jobs.get_nowait()
                except queue.Empty:
                    return
                if cancelled.is_set():          # the caller timed out: do not start it late
                    continue
                try:
                    box.put(fn())
                except Exception as e:
                    box.put({"error": f"{type(e).__name__}: {e}"})
        finally:
            self._busy = False

    def call(self, fn, timeout_s: float = UI_CALL_TIMEOUT_S) -> dict:
        if not self.installed:
            return {"error": "no NIS main-thread hook: this call crashes NIS from the worker thread"}
        box, cancelled = queue.Queue(), threading.Event()
        self._jobs.put((fn, box, cancelled))
        try:
            return box.get(timeout=timeout_s)
        except queue.Empty:
            cancelled.set()
            return {"error": f"NIS's main thread did not run the call within {timeout_s:.0f} s"}


class Bridge:
    """The operations, as plain methods returning JSON-able dicts."""

    def __init__(self, macro: Macro, ui: MainThread | None = None):
        self.m = macro
        self.ui = ui or MainThread()        # not installed: UI calls are refused

    def status(self) -> dict:
        x, y, z = ct.c_double(), ct.c_double(), ct.c_double()
        rc_xy = self.m.StgGetPosXY(ct.byref(x), ct.byref(y))
        rc_z = self.m.StgGetPosZ(ct.byref(z), 0)
        cur = ct.create_unicode_buffer(255)
        self.m.GetCurrentObjName(cur)
        names = {}
        for i in range(0, self.m.Stg_GetNosepiecePositions() + 1):  # 0-based, checked on the scope 2026-10-13
            b = ct.create_unicode_buffer(255)
            if self.m.Stg_GetNosepieceObjectiveName(i, b, 255) == 1 and b.value:
                names[str(i)] = b.value
        return {"version": VERSION, "pid": os.getpid(),
                "xy_um": [x.value, y.value], "xy_rc": rc_xy,
                "z_um": z.value, "z_rc": rc_z,
                "nosepiece_position": self.m.Stg_GetNosepiecePosition(),
                "current_objective": cur.value, "nosepiece_objectives": names,
                "nd_running": bool(self.m.ND_IsInExperimentCapture()),
                "estop_engaged": _estop_engaged()}

    def _refuse_if_stopped(self, what: str) -> dict | None:
        if _estop_engaged():
            return {"error": f"e-stop engaged - {what} refused", "estop_engaged": True}
        return None

    def move(self, dx: float, dy: float) -> dict:
        if (r := self._refuse_if_stopped("move")):
            return r
        if max(abs(dx), abs(dy)) > MAX_STEP_UM:
            return {"error": f"step over {MAX_STEP_UM} um refused"}
        before = self.status()["xy_um"]
        rc = self.m.StgMoveXY(float(dx), float(dy), MOVE_RELATIVE)
        return {"rc": rc, "before_um": before, "after_um": self.status()["xy_um"]}

    def objective(self, name: str) -> dict:
        if (r := self._refuse_if_stopped("objective change")):
            return r
        if magnification(name) not in ALLOWED_MAGNIFICATIONS:
            allowed = "/".join(f"{m:g}x" for m in ALLOWED_MAGNIFICATIONS)
            return {"error": f"only {allowed} objectives are allowed, got {name!r}"}
        before = self.status()
        # Two separate things, both needed (seen on the scope 2026-10-13/14):
        # Stg_SetNosepiecePosition turns the turret but NIS's objective name
        # and calibration stay on the old objective, and ChangeObjective
        # updates the name and calibration but leaves the turret where it
        # is. So turn the turret, wait for it to arrive, and only then tell
        # NIS - never switch the calibration for a turret that did not move.
        targets = [int(i) for i, n in before["nosepiece_objectives"].items() if n == name]
        if len(targets) != 1:
            return {"error": f"{name!r} is not on the nosepiece exactly once",
                    "nosepiece_objectives": before["nosepiece_objectives"]}
        rc = self.m.Stg_SetNosepiecePosition(targets[0])
        deadline = time.time() + TURRET_SETTLE_S
        after = self.status()
        while rc == 1 and time.time() < deadline and after["nosepiece_position"] != targets[0]:
            time.sleep(TURRET_POLL_S)
            after = self.status()
        rc_calibration = None
        if after["nosepiece_position"] == targets[0]:
            rc_calibration = self.m.ChangeObjective(name)
            after = self.status()
        arrived = (after["nosepiece_position"] == targets[0]
                   and after["current_objective"] == name)
        return {"rc": rc, "rc_calibration": rc_calibration, "arrived": arrived,
                "before": before["current_objective"], "after": after["current_objective"],
                "nosepiece_before": before["nosepiece_position"],
                "nosepiece_after": after["nosepiece_position"]}

    def optical_configs(self) -> dict:
        names = []
        for i in range(0, self.m.GetOptConfCount() + 1):    # index base unverified: try 0..n
            b = ct.create_unicode_buffer(255)
            self.m.GetOptConfName(i, b, 255)
            if b.value and b.value not in names:
                names.append(b.value)
        return {"optical_configs": names}

    # No select: SelectOptConf called from this worker thread switched the
    # devices ("end success" in NIS's log) and then froze NIS's main thread
    # until the bridge was shut down (2026-10-15). Set the optical
    # configuration in NIS, or find a way to run it on the UI thread first.

    def capture(self, path: str) -> dict:
        if (r := self._refuse_if_stopped("capture")):
            return r
        if not path.lower().endswith(".nd2"):
            return {"error": "path must end in .nd2"}
        if os.path.exists(path):
            return {"error": f"{path} already exists - refusing to overwrite"}

        def on_main_thread() -> dict:
            if _estop_engaged():                # re-check: the call may have waited in the queue
                return {"error": "e-stop engaged - capture refused", "estop_engaged": True}
            rc_cap = self.m.Capture()
            rc_save = self.m.ImageSaveAs(path, ND2_ALL_LAYERS, 0)
            rc_close = self.m.CloseCurrentDocument(QUERYSAVE_NO)
            return {"rc_capture": rc_cap, "rc_save": rc_save, "rc_close": rc_close,
                    "path": path, "saved": os.path.exists(path)}
        return self.ui.call(on_main_thread)

    def nd_run(self, experiment: str) -> dict:
        if (r := self._refuse_if_stopped("ND run")):
            return r

        def on_main_thread() -> dict:
            if _estop_engaged():
                return {"error": "e-stop engaged - ND run refused", "estop_engaged": True}
            if self.m.ND_IsInExperimentCapture():
                return {"error": "an ND experiment is already running"}
            # ND_LoadExperiment and ND_RunExperiment have no documented return
            # value (NIS macro reference), and on the scope ND_LoadExperiment
            # returned 0 for a template that does load (2026-10-30). So the
            # codes are reported, not checked: NIS runs whatever the ND
            # Acquisition window holds after the load.
            rc_load = self.m.ND_LoadExperiment(experiment)
            t0 = time.time()
            rc_run = self.m.ND_RunExperiment(0)          # 0: save to disk, do not open
            return {"rc_load": rc_load, "rc_run": rc_run,
                    "call_returned_after_s": round(time.time() - t0, 1),
                    "nd_running": bool(self.m.ND_IsInExperimentCapture())}
        return self.ui.call(on_main_thread)

    def nd_finish(self) -> dict:
        # Always allowed, e-stop or not: it only ends a run early.
        def on_main_thread() -> dict:
            rc = self.m.ND_FinishExperiment()
            return {"rc": rc, "nd_running": bool(self.m.ND_IsInExperimentCapture())}
        return self.ui.call(on_main_thread)


# Routes: (method, path) -> function(bridge, body) -> dict
ROUTES = {
    ("GET", "/status"): lambda b, body: b.status(),
    ("POST", "/move"): lambda b, body: b.move(float(body.get("dx", 0)), float(body.get("dy", 0))),
    ("POST", "/objective"): lambda b, body: b.objective(str(body.get("name", ""))),
    ("GET", "/optconfs"): lambda b, body: b.optical_configs(),
    ("POST", "/capture"): lambda b, body: b.capture(str(body.get("path", ""))),
    ("POST", "/nd/run"): lambda b, body: b.nd_run(str(body.get("experiment", ""))),
    ("POST", "/nd/finish"): lambda b, body: b.nd_finish(),
}


def make_handler(jobs: "queue.Queue", stop: threading.Event, timeout_s: float = 7200.0):
    class Handler(http.server.BaseHTTPRequestHandler):
        def _answer(self, code: int, out: dict) -> None:
            body = (json.dumps(out) + "\n").encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _dispatch(self, method: str) -> None:
            path = self.path.split("?")[0]
            if method == "POST" and path == "/shutdown":
                stop.set()
                return self._answer(200, {"stopping": True})
            fn = ROUTES.get((method, path))
            if fn is None:
                return self._answer(404, {"error": f"no route {method} {path}",
                                          "routes": [f"{m} {p}" for m, p in ROUTES]})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            except ValueError:
                return self._answer(400, {"error": "body is not JSON"})
            box = queue.Queue()
            jobs.put((fn, body, box))
            try:
                self._answer(200, box.get(timeout=timeout_s))
            except queue.Empty:
                self._answer(504, {"error": "timed out waiting for NIS"})

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def log_message(self, *args):      # keep the JOBS log readable
            pass

    return Handler


def serve(bridge: Bridge, port: int = PORT, should_abort=lambda: False,
          stop: threading.Event | None = None, on_ready=None,
          idle_stop_s: float | None = None) -> None:
    """Serve until /shutdown, should_abort() or `stop` is set, or until no
    request has arrived for idle_stop_s (default IDLE_STOP_S). NIS calls run
    here, on the calling thread; HTTP requests are handled on others."""
    idle_stop_s = IDLE_STOP_S if idle_stop_s is None else idle_stop_s
    last_request = time.time()
    jobs = queue.Queue()
    stop = stop or threading.Event()
    # On Windows SO_REUSEADDR (the default here) lets a second bridge bind the
    # same port, so re-running the job started a duplicate whose shutdown had
    # to be sent twice (seen 2026-10-15). Without it a second run fails to bind.
    http.server.ThreadingHTTPServer.allow_reuse_address = False
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), make_handler(jobs, stop))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"NIS bridge v{VERSION} listening on 127.0.0.1:{srv.server_address[1]}, pid {os.getpid()}")
    if on_ready:
        on_ready(srv.server_address[1])
    try:
        while not stop.is_set() and not should_abort():
            try:
                fn, body, box = jobs.get(timeout=0.2)
            except queue.Empty:
                if time.time() - last_request > idle_stop_s:
                    print(f"NIS bridge idle for {idle_stop_s:.0f} s - stopping")
                    break
                continue
            try:
                box.put(fn(bridge, body))
            except Exception as e:
                box.put({"error": f"{type(e).__name__}: {e}"})
            last_request = time.time()
    finally:
        srv.shutdown()
        srv.server_close()
        print("NIS bridge stopped")


def run(imgs: "tuple[limjob.Image]", Job: "limjob.JobParam", macro: "limjob.MacroParam",
        ctx: "limjob.RunContext"):
    """JOBS entry point. Serving on the job's own thread froze NIS (step 0 of
    docs/nis-bridge.md, 2026-10-15): a Python task runs on NIS's main thread,
    so a loop that never returns blocks the UI. So serve on a worker thread
    and return at once; the job shows as finished while the bridge keeps
    listening. Stop it with POST /shutdown (or the idle backstop). Whether
    NIS macro calls work from a non-main thread is UNVERIFIED: try status
    first, then the moves. The hints are the JOBS template's own, as strings
    so this file imports off NIS."""
    ui = MainThread()
    try:
        ui.install()                # run() is on NIS's main thread: hook it for UI calls
    except Exception as e:
        print(f"NIS bridge: no main-thread hook ({type(e).__name__}: {e}); capture and ND calls refused")
    threading.Thread(target=serve, args=(Bridge(Macro(), ui),), name="nis-bridge",
                     daemon=True).start()
