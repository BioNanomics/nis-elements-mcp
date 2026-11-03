# IMPORTANT: 'limjob' must be imported like this (not from nor as)
try:
    import limjob
except ImportError:          # off the microscope (tests, python NISMCPServer.py)
    limjob = None

# NISMCPServer.py
# ------------------------------------------------------------
# An MCP server that runs inside NIS-Elements. The MCPServer job (see
# install_job.py) loads this file and calls run(), which serves MCP's
# Streamable HTTP transport on http://127.0.0.1:8767/mcp and returns, so
# NIS stays responsive. Standard library only.
#
# Tools: echo, status, move_relative, change_objective, list_optical_configs,
# capture, run_experiment, finish_experiment, estop.
#
# Safety:
#   * nothing moves or starts while the e-stop file exists (~/.nis-mcp/ESTOP).
#     The estop tool engages it; only a person releases it, by deleting the file
#   * XY moves are relative, at most MAX_STEP_UM per axis; Z never moves
#   * objective changes only to ALLOWED_MAGNIFICATIONS (4x/10x, whose
#     working distances cannot reach the dish)
#   * captures go only into CAPTURE_DIR, by plain file name, never overwriting
#   * ND runs only of ALLOWED_EXPERIMENTS, when it lists any
#   * finish_experiment and estop are always allowed: they only stop things
# To change a limit, edit it below and save: the server reloads.
#
# Threading: tools run on the HTTP threads, one at a time (NIS.held). Capture,
# saving and the ND calls open NIS windows and crashed NIS when called from
# a worker thread (2026-10-16), so they run on NIS's main thread, through a
# Win32 timer that run() installs there (MainThread).
#
# Upgrade: save this file. A watcher reloads it within seconds; if the new
# code does not load, the old server keeps running. Running the MCPServer
# job again also works: run() first asks a server on the port to stop.
# A reload runs run() on the watcher thread, not NIS's main thread, so the
# old server hands its main-thread hook to the new one.
# Stop:    POST http://127.0.0.1:8767/shutdown. Never press JOBS Abort.
# Off NIS: python NISMCPServer.py   (serves in the foreground)
# ------------------------------------------------------------
import contextlib
import ctypes as ct
import datetime
import http.server
import importlib.util
import json
import os
import queue
import re
import threading
import time
import urllib.request
from pathlib import Path

HOST = "127.0.0.1"
PORT = int(os.environ.get("NIS_MCP_PORT", "").strip() or 8767)
NAME, VERSION = "nis-mcp-server", "0.4.0"
WATCH_S = 1.0                   # how often to look for a changed file
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")   # newest first

ALLOWED_MAGNIFICATIONS = (4.0, 10.0)
MAX_STEP_UM = 1000.0            # largest XY step per call, per axis
CAPTURE_DIR = Path.home() / "nis-mcp" / "captures"
ALLOWED_EXPERIMENTS = ()        # saved ND experiment names; empty: any
ESTOP = Path(os.environ.get("NIS_ESTOP_FILE", "").strip() or Path.home() / ".nis-mcp" / "ESTOP")
TURRET_SETTLE_S = 25.0          # how long to wait for the nosepiece to arrive
TURRET_POLL_S = 0.25
BUSY_WAIT_S = 30.0              # how long a tool waits for another one to finish with NIS
UI_TICK_MS = 50                 # how often NIS's main thread looks for queued calls
UI_CALL_TIMEOUT_S = 600.0       # give up waiting for the main thread after this long
ND_RUN_TIMEOUT_S = 4 * 3600.0   # ND_RunExperiment returns only when the run ends
MOVE_RELATIVE = 1               # StgMoveXY 'relative' argument
ND2_ALL_LAYERS = 14             # ImageSaveAs ImType: all layers (ND2 file)
QUERYSAVE_NO = 2                # CloseCurrentDocument: close without asking
# "4x" / "10x" as a whole magnification - not the 4x inside "14x" or "40x"
_MAG = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*[xX](?![a-zA-Z0-9])")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,80}$")


class NISBusy(RuntimeError):
    """Another tool has been using NIS for longer than BUSY_WAIT_S."""


class NIS:
    """The NIS macro functions used here, from g5_regprocs.dll through ctypes
    (signatures from the NIS 6.20 macro reference; char* is wchar_t*).
    NIS is not known to be thread-safe: use it only inside `held()`."""

    SIGNATURES = {
        "StgGetPosXY": [ct.POINTER(ct.c_double), ct.POINTER(ct.c_double)],
        "StgGetPosZ": [ct.POINTER(ct.c_double), ct.c_int],
        "StgMoveXY": [ct.c_double, ct.c_double, ct.c_int],
        "GetCurrentObjName": [ct.c_wchar_p],
        "Stg_GetNosepiecePosition": [],
        "Stg_GetNosepiecePositions": [],
        "Stg_GetNosepieceObjectiveName": [ct.c_int, ct.c_wchar_p, ct.c_int],
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
        self.lock = threading.Lock()
        if dll is None:
            dll = ct.cdll.g5_regprocs   # only loads inside nis_ar.exe
            for name, args in self.SIGNATURES.items():
                getattr(dll, name).argtypes, getattr(dll, name).restype = args, ct.c_int
        self.dll = dll

    def __getattr__(self, name):
        return getattr(self.dll, name)

    @contextlib.contextmanager
    def held(self):
        """NIS to this caller alone. An ND run holds it until the run ends,
        so other tools say so instead of waiting for hours."""
        if not self.lock.acquire(timeout=BUSY_WAIT_S):
            raise NISBusy(f"another tool has been using NIS for over {BUSY_WAIT_S:g} s "
                          "(an ND run holds it until the run ends)")
        try:
            yield self
        finally:
            self.lock.release()

    def position(self) -> "tuple[float, float, float]":
        """Stage x, y and z in um."""
        x, y, z = ct.c_double(), ct.c_double(), ct.c_double()
        self.StgGetPosXY(ct.byref(x), ct.byref(y))
        self.StgGetPosZ(ct.byref(z), 0)
        return x.value, y.value, z.value

    def turret(self) -> "tuple[int, str, dict[int, str]]":
        """(nosepiece position, NIS's current objective, {position: objective name})."""
        cur = ct.create_unicode_buffer(255)
        self.GetCurrentObjName(cur)
        names = {}
        for i in range(self.Stg_GetNosepiecePositions() + 1):    # 0-based, checked on the scope 2026-10-13
            b = ct.create_unicode_buffer(255)
            if self.Stg_GetNosepieceObjectiveName(i, b, 255) == 1 and b.value:
                names[i] = b.value
        return self.Stg_GetNosepiecePosition(), cur.value, names


_nis = None


def nis() -> NIS:
    global _nis
    if _nis is None:
        _nis = NIS()
    return _nis


class MainThread:
    """Runs calls on NIS's main (UI) thread.

    run() is the one place this code is on the main thread, so it calls
    install(), which registers a Win32 thread timer there. NIS's own message
    loop then calls _tick on the main thread every UI_TICK_MS, and _tick runs
    what call() queued. Not installed: call() refuses.
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
                if cancelled.is_set():          # the caller gave up: do not start it late
                    continue
                try:
                    box.put(fn())
                except Exception as e:
                    box.put({"error": f"{type(e).__name__}: {e}"})
        finally:
            self._busy = False

    def call(self, fn, timeout_s: float = UI_CALL_TIMEOUT_S) -> dict:
        if not self.installed:
            return {"error": "no hook on NIS's main thread, which this call needs: run the MCPServer job in NIS"}
        box, cancelled = queue.Queue(), threading.Event()
        self._jobs.put((fn, box, cancelled))
        try:
            return box.get(timeout=timeout_s)
        except queue.Empty:
            cancelled.set()
            return {"error": f"NIS's main thread did not run the call within {timeout_s:.0f} s"}


_ui = MainThread()                      # replaced by run()


def magnification(name: str) -> "float | None":
    """The one magnification in an objective name ('PLAN APO 10x' -> 10.0), else None."""
    mags = {float(m) for m in _MAG.findall(name)}
    return mags.pop() if len(mags) == 1 else None


def estop_engaged() -> bool:
    try:
        return ESTOP.exists()
    except OSError:
        return True             # cannot tell -> treat as engaged


def _stopped(what: str) -> "dict | None":
    if estop_engaged():
        return {"error": f"e-stop engaged ({ESTOP}) - {what} refused", "estop_engaged": True}
    return None


def echo(args: dict) -> dict:
    if not isinstance(args.get("text"), str):
        return {"error": "echo needs a string 'text'"}
    return {"text": args["text"]}


def status(args: dict) -> dict:
    with nis().held() as m:
        x, y, z = m.position()
        pos, cur, names = m.turret()
        nd = bool(m.ND_IsInExperimentCapture())
    return {"xy_um": [x, y], "z_um": z, "objective": cur, "nosepiece_position": pos,
            "nosepiece": names, "nd_running": nd, "estop_engaged": estop_engaged()}


def move_relative(args: dict) -> dict:
    try:
        dx, dy = float(args["dx_um"]), float(args["dy_um"])
    except (KeyError, TypeError, ValueError):
        return {"error": "move_relative needs numbers 'dx_um' and 'dy_um'"}
    if (r := _stopped("move")):
        return r
    if not (abs(dx) <= MAX_STEP_UM and abs(dy) <= MAX_STEP_UM):     # also refuses nan
        return {"error": f"steps over {MAX_STEP_UM:g} um per axis are refused, got ({dx:g}, {dy:g})"}
    with nis().held() as m:
        before = m.position()[:2]
        rc = m.StgMoveXY(dx, dy, MOVE_RELATIVE)
        after = m.position()[:2]
    return {"before_um": list(before), "after_um": list(after), "rc": rc}


def change_objective(args: dict) -> dict:
    name = args.get("name")
    if not isinstance(name, str) or not name:
        return {"error": "change_objective needs the objective's 'name'"}
    if (r := _stopped("objective change")):
        return r
    if magnification(name) not in ALLOWED_MAGNIFICATIONS:
        allowed = "/".join(f"{m:g}x" for m in ALLOWED_MAGNIFICATIONS)
        return {"error": f"only {allowed} objectives are allowed, got {name!r}"}
    with nis().held() as m:
        pos, before, names = m.turret()
        targets = [i for i, n in names.items() if n == name]
        if len(targets) != 1:
            return {"error": f"{name!r} is not on the nosepiece exactly once",
                    "current": before, "nosepiece": names}
        target = targets[0]
        if pos == target and before == name:
            return {"objective": name, "changed": False}
        # Both calls are needed (seen on the scope 2026-10-13/14):
        # Stg_SetNosepiecePosition turns the turret but leaves NIS's objective
        # and calibration alone; ChangeObjective switches those but not the
        # turret. Turn, wait for arrival, and only then tell NIS.
        rc = m.Stg_SetNosepiecePosition(target)
        deadline = time.time() + TURRET_SETTLE_S
        while rc == 1 and pos != target and time.time() < deadline:
            time.sleep(TURRET_POLL_S)
            pos = m.Stg_GetNosepiecePosition()
        rc_calibration = m.ChangeObjective(name) if pos == target else None
        pos, after, _ = m.turret()
    out = {"objective": after, "before": before, "changed": after != before,
           "nosepiece_position": pos, "rc": rc, "rc_calibration": rc_calibration}
    if pos != target or after != name:
        out["error"] = f"did not arrive at {name!r}"
    return out


def list_optical_configs(args: dict) -> dict:
    names = []
    with nis().held() as m:
        for i in range(m.GetOptConfCount() + 1):        # index base unverified: try 0..n
            b = ct.create_unicode_buffer(255)
            m.GetOptConfName(i, b, 255)
            if b.value and b.value not in names:
                names.append(b.value)
    return {"optical_configs": names}

# No select: SelectOptConf from a worker thread switched the devices and then
# froze NIS's main thread (2026-10-15). Set the optical configuration in NIS.


def capture(args: dict) -> dict:
    name = args.get("filename")
    if not isinstance(name, str):
        return {"error": "capture needs a 'filename'"}
    stem = name[:-4] if name.lower().endswith(".nd2") else name
    if not _SAFE_NAME.match(stem) or ".." in stem:
        return {"error": "filename must be a plain name (letters, digits, space, _ . -), no folders"}
    if (r := _stopped("capture")):
        return r
    path = CAPTURE_DIR / f"{stem}.nd2"
    if path.exists():
        return {"error": f"{path} already exists - refusing to overwrite"}
    path.parent.mkdir(parents=True, exist_ok=True)
    with nis().held() as m:
        def on_main_thread() -> dict:
            if (r := _stopped("capture")):          # re-check: the call may have waited in the queue
                return r
            rc_capture = m.Capture()
            rc_save = m.ImageSaveAs(str(path), ND2_ALL_LAYERS, 0)
            rc_close = m.CloseCurrentDocument(QUERYSAVE_NO)
            return {"path": str(path), "saved": path.exists(),
                    "rc_capture": rc_capture, "rc_save": rc_save, "rc_close": rc_close}
        out = _ui.call(on_main_thread, timeout_s=300)
    if out.get("saved") is False:
        out["error"] = "NIS did not save the file"
    return out


def run_experiment(args: dict) -> dict:
    name = args.get("experiment")
    if not isinstance(name, str) or not name:
        return {"error": "run_experiment needs the saved 'experiment' name"}
    if ALLOWED_EXPERIMENTS and name not in ALLOWED_EXPERIMENTS:
        return {"error": f"ND experiment {name!r} is not in ALLOWED_EXPERIMENTS ({', '.join(ALLOWED_EXPERIMENTS)})"}
    if (r := _stopped("ND run")):
        return r
    with nis().held() as m:
        def on_main_thread() -> dict:
            if (r := _stopped("ND run")):
                return r
            if m.ND_IsInExperimentCapture():
                return {"error": "an ND experiment is already running"}
            # ND_LoadExperiment and ND_RunExperiment have no documented return
            # value, and on the scope ND_LoadExperiment returned 0 for a
            # template that does load (2026-10-30). So the codes are reported,
            # not checked: NIS runs whatever the ND Acquisition window holds.
            rc_load = m.ND_LoadExperiment(name)
            t0 = time.time()
            rc_run = m.ND_RunExperiment(0)          # 0: save to disk, do not open
            return {"experiment": name, "rc_load": rc_load, "rc_run": rc_run,
                    "call_returned_after_s": round(time.time() - t0, 1),
                    "nd_running": bool(m.ND_IsInExperimentCapture())}
        return _ui.call(on_main_thread, timeout_s=ND_RUN_TIMEOUT_S)


def finish_experiment(args: dict) -> dict:
    # Always allowed, e-stop or not: it only ends a run early.
    with nis().held() as m:
        def on_main_thread() -> dict:
            rc = m.ND_FinishExperiment()
            return {"rc": rc, "nd_running": bool(m.ND_IsInExperimentCapture())}
        return _ui.call(on_main_thread)


def estop(args: dict) -> dict:
    action = args.get("action", "status")
    if action == "engage":
        info = {"engaged_at": datetime.datetime.now().isoformat(timespec="seconds"),
                "by": f"pid {os.getpid()} ({NAME})", "reason": str(args.get("reason") or "requested via MCP")}
        ESTOP.parent.mkdir(parents=True, exist_ok=True)
        ESTOP.write_text(json.dumps(info, indent=2), encoding="utf-8")
    elif action != "status":
        return {"error": f"unknown action {action!r}: use 'engage' or 'status'. "
                         f"Releasing is for a person: delete {ESTOP}"}
    out = {"engaged": estop_engaged(), "flag_file": str(ESTOP)}
    if out["engaged"]:
        try:
            out["detail"] = json.loads(ESTOP.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            out["detail"] = "(e-stop file present but unreadable)"
        out["release"] = f"a person deletes {ESTOP}"
    return out


def _schema(**props) -> dict:
    """{"type": "object"} with these properties, all required."""
    return {"type": "object", "properties": props, "required": list(props)}


_NUMBER, _STRING = {"type": "number"}, {"type": "string"}

TOOLS = [
    ({"name": "echo",
      "description": "Return the text unchanged. Checks that the MCP server inside NIS-Elements answers.",
      "inputSchema": _schema(text=_STRING)},
     echo),
    ({"name": "status",
      "description": "Read-only: stage XY and Z (um), the current objective, the objective at each "
                     "nosepiece position, whether an ND experiment is running, whether the e-stop is engaged.",
      "inputSchema": _schema()},
     status),
    ({"name": "move_relative",
      "description": f"Move the XY stage by (dx_um, dy_um) microns from where it is, at most "
                     f"{MAX_STEP_UM:g} um per axis. Returns the position before and after; treat "
                     "after_um as the truth. Z cannot be moved.",
      "inputSchema": _schema(dx_um=_NUMBER, dy_um=_NUMBER)},
     move_relative),
    ({"name": "change_objective",
      "description": "Turn the nosepiece to an objective and switch NIS's calibration to it. Only the 4x "
                     "and 10x objectives are allowed. Give the objective's name exactly as on the "
                     "nosepiece; a wrong name returns the list.",
      "inputSchema": _schema(name=_STRING)},
     change_objective),
    ({"name": "list_optical_configs",
      "description": "Read-only: the optical configurations defined in NIS. None can be selected from "
                     "here: set it in NIS before capture.",
      "inputSchema": _schema()},
     list_optical_configs),
    ({"name": "capture",
      "description": "Capture one image with the optical configuration active in NIS and save it as ND2 "
                     "in the capture folder. 'filename' is a plain name such as \"aml18_check\", no "
                     "folders; .nd2 is added. An existing file is never overwritten. Returns the path.",
      "inputSchema": _schema(filename=_STRING)},
     capture),
    ({"name": "run_experiment",
      "description": "Start an ND experiment that was set up and SAVED in NIS's ND Acquisition window, by "
                     "its saved name, with exactly its saved settings. NIS does not say whether the name "
                     "was found: if not, it runs whatever the ND Acquisition window holds, so check the "
                     "name with the operator first. Returns only when the run ends; meanwhile the other "
                     "tools answer that NIS is busy.",
      "inputSchema": _schema(experiment=_STRING)},
     run_experiment),
    ({"name": "finish_experiment",
      "description": "End the running ND experiment after its current loop; NIS keeps the frames so far. "
                     "While run_experiment is still waiting this answers busy: use NIS's Finish button.",
      "inputSchema": _schema()},
     finish_experiment),
    ({"name": "estop",
      "description": "EMERGENCY STOP. Call it the moment anything looks wrong (an unexpected move, a "
                     "position that does not match, the operator says stop): action \"engage\" forbids "
                     "every move, objective change, capture and ND run until a person releases it. A "
                     "move already under way finishes. action \"status\" reports. It cannot be released "
                     "from here.",
      "inputSchema": {"type": "object",
                      "properties": {"action": {"type": "string", "enum": ["engage", "status"]},
                                     "reason": _STRING},
                      "required": ["action"]}},
     estop),
]


def call_tool(name: str, args: dict) -> dict:
    fn = dict((t["name"], f) for t, f in TOOLS)[name]       # KeyError: unknown tool
    try:
        out = fn(args)
    except Exception as e:
        out = {"error": f"{type(e).__name__}: {e}"}
    text = out["text"] if fn is echo and "error" not in out else json.dumps(out, indent=1)
    return {"content": [{"type": "text", "text": text}], "isError": "error" in out}


def handle(msg: dict) -> "dict | None":
    """One JSON-RPC message -> its response, or None for a notification or a response."""
    if "method" not in msg:
        return None
    method, params, mid = msg["method"], msg.get("params") or {}, msg.get("id")
    if mid is None:
        return None                     # notifications/initialized and friends
    try:
        if method == "initialize":
            asked = params.get("protocolVersion")
            result = {"protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                      "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": NAME, "version": VERSION}}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [t for t, _ in TOOLS]}
        elif method == "tools/call":
            try:
                result = call_tool(params.get("name"), params.get("arguments") or {})
            except KeyError:
                return _error(mid, -32602, f"unknown tool: {params.get('name')}")
        else:
            return _error(mid, -32601, f"method not found: {method}")
    except Exception as e:
        return _error(mid, -32603, f"{type(e).__name__}: {e}")
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _local_origin(origin: "str | None") -> bool:
    """No Origin (not a browser) or a localhost page. Blocks DNS rebinding."""
    if not origin:
        return True
    host = origin.split("://", 1)[-1].split("/", 1)[0].rsplit(":", 1)[0]
    return host in ("localhost", "127.0.0.1", "[::1]")


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, code: int, out=None) -> None:
        body = json.dumps(out).encode() if out is not None else b""
        self.send_response(code)
        if out is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if not _local_origin(self.headers.get("Origin")):
            return self._send(403, {"error": "origin not allowed"})
        path = self.path.split("?")[0]
        if path == "/shutdown" and not self.headers.get("Origin"):
            self._send(200, {"stopping": True})
            return threading.Thread(target=stop, args=(self.server,), daemon=True).start()
        if path != "/mcp":
            return self._send(404, {"error": f"no route {path}"})
        try:
            msg = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        except ValueError:
            return self._send(400, _error(None, -32700, "parse error"))
        if not isinstance(msg, dict):
            return self._send(400, _error(None, -32600, "expected one JSON-RPC object"))
        out = handle(msg)
        self._send(202) if out is None else self._send(200, out)

    def do_GET(self):                   # no server-to-client stream
        self.send_response(405)
        self.send_header("Allow", "POST")
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_DELETE = do_GET                  # no sessions to end

    def log_message(self, *args):       # keep the JOBS log readable
        pass


def start(port: int = PORT) -> http.server.ThreadingHTTPServer:
    """Bind and serve on a daemon thread. Without SO_REUSEADDR a second
    server cannot bind the same port on Windows. Elsewhere it never allows
    that, and without it a restart cannot bind the port for a minute."""
    http.server.ThreadingHTTPServer.allow_reuse_address = os.name != "nt"
    srv = http.server.ThreadingHTTPServer((HOST, port), Handler)
    srv.done = threading.Event()
    threading.Thread(target=srv.serve_forever, name="nis-mcp-server", daemon=True).start()
    return srv


def stop(srv: http.server.ThreadingHTTPServer) -> None:
    """Stop serving and free the port."""
    srv.shutdown()
    srv.server_close()
    srv.done.set()


def watch(srv: http.server.ThreadingHTTPServer, run_args: tuple) -> None:
    """While srv serves, reload this file when it changes. The new code's
    run() replaces this server. If the new code does not load, keep serving."""
    path = Path(__file__)

    def stamp():
        try:
            st = path.stat()
            return st.st_mtime_ns, st.st_size
        except OSError:
            return None

    seen = stamp()
    while not srv.done.wait(WATCH_S):
        now = stamp()
        if now is None or now == seen:
            continue
        time.sleep(0.5)                 # let the editor finish writing
        if stamp() != now:
            continue                    # still changing: look again
        seen = now
        try:
            spec = importlib.util.spec_from_file_location("NISMCPServer", path)
            new = importlib.util.module_from_spec(spec)
            # Compile the file itself: __pycache__ is trusted by size and
            # whole-second mtime, so a same-size edit within a second ran old code.
            exec(compile(path.read_bytes(), str(path), "exec"), new.__dict__)
        except Exception as e:
            print(f"NIS MCP server: {path.name} changed but does not load "
                  f"({type(e).__name__}: {e}) - still serving {VERSION}")
            continue
        print(f"NIS MCP server: {path.name} changed - reloading")
        new.run(*run_args, ui=_ui)      # this thread is not NIS's main thread: pass the hook on
        return


def stop_running(port: int = PORT) -> bool:
    """Ask a server already on the port to stop. True if one answered."""
    try:
        urllib.request.urlopen(urllib.request.Request(f"http://{HOST}:{port}/shutdown", method="POST"),
                               timeout=2).read()
        return True
    except OSError:
        return False


def run(imgs: "tuple[limjob.Image]", Job: "limjob.JobParam", macro: "limjob.MacroParam",
        ctx: "limjob.RunContext", ui: "MainThread | None" = None):
    """JOBS entry point. Serving on the job's own thread (NIS's main thread)
    froze NIS, so serve on a daemon thread and return at once. The job calls
    this on NIS's main thread, so hook it here for capture and the ND calls;
    a reload calls it on the watcher thread and passes the old hook as `ui`."""
    global _ui
    if ui is None:
        ui = MainThread()
        if threading.current_thread().name == "nis-mcp-watch":     # reloaded by a server older than 0.4.0
            print("NIS MCP server: no main-thread hook - run the MCPServer job again for capture and ND runs")
        else:
            try:
                ui.install()
            except Exception as e:
                print(f"NIS MCP server: no main-thread hook ({type(e).__name__}: {e}); capture and ND runs refused")
    _ui = ui
    if stop_running():
        print("NIS MCP server: stopped the old one")
    for attempt in range(20):           # the old socket can take a moment to close
        try:
            srv = start()
            break
        except OSError:
            time.sleep(0.25)
    else:
        print(f"NIS MCP server: port {PORT} is busy")
        return
    threading.Thread(target=watch, args=(srv, (imgs, Job, macro, ctx)), name="nis-mcp-watch",
                     daemon=True).start()
    print(f"NIS MCP server {VERSION} on http://{HOST}:{srv.server_address[1]}/mcp, reloads on save")


if __name__ == "__main__":
    srv = start()
    print(f"NIS MCP server {VERSION} on http://{HOST}:{srv.server_address[1]}/mcp - Ctrl+C to stop")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        stop(srv)
