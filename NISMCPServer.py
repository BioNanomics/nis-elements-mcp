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
# Tools: echo, change_objective.
#
# Safety: objective changes only to ALLOWED_MAGNIFICATIONS (4x/10x, whose
# working distances cannot reach the dish), and nothing moves while the
# e-stop file exists (~/.nis-mcp/ESTOP, the same file as nis-elements-mcp).
#
# Upgrade: edit this file and run the MCPServer job again. run() first asks
# a server already on the port to stop, then starts the new code.
# Stop:    POST http://127.0.0.1:8767/shutdown. Never press JOBS Abort.
# Off NIS: python NISMCPServer.py   (serves in the foreground)
# ------------------------------------------------------------
import ctypes as ct
import http.server
import json
import os
import re
import threading
import time
import urllib.request
from pathlib import Path

HOST = "127.0.0.1"
PORT = int(os.environ.get("NIS_MCP_PORT", "").strip() or 8767)
NAME, VERSION = "nis-mcp-server", "0.2.0"
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")   # newest first

ALLOWED_MAGNIFICATIONS = (4.0, 10.0)
ESTOP = Path(os.environ.get("NIS_ESTOP_FILE", "").strip() or Path.home() / ".nis-mcp" / "ESTOP")
TURRET_SETTLE_S = 25.0          # how long to wait for the nosepiece to arrive
TURRET_POLL_S = 0.25
# "4x" / "10x" as a whole magnification - not the 4x inside "14x" or "40x"
_MAG = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*[xX](?![a-zA-Z0-9])")


class NIS:
    """The NIS macro functions used here, from g5_regprocs.dll through ctypes
    (signatures from the NIS 6.20 macro reference; char* is wchar_t*).
    NIS is not known to be thread-safe: hold `lock` around every use."""

    SIGNATURES = {
        "GetCurrentObjName": [ct.c_wchar_p],
        "Stg_GetNosepiecePosition": [],
        "Stg_GetNosepiecePositions": [],
        "Stg_GetNosepieceObjectiveName": [ct.c_int, ct.c_wchar_p, ct.c_int],
        "Stg_SetNosepiecePosition": [ct.c_int],
        "ChangeObjective": [ct.c_wchar_p],
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


def magnification(name: str) -> "float | None":
    """The one magnification in an objective name ('PLAN APO 10x' -> 10.0), else None."""
    mags = {float(m) for m in _MAG.findall(name)}
    return mags.pop() if len(mags) == 1 else None


def estop_engaged() -> bool:
    try:
        return ESTOP.exists()
    except OSError:
        return True             # cannot tell -> treat as engaged


def echo(args: dict) -> dict:
    if not isinstance(args.get("text"), str):
        return {"error": "echo needs a string 'text'"}
    return {"text": args["text"]}


def change_objective(args: dict) -> dict:
    name = args.get("name")
    if not isinstance(name, str) or not name:
        return {"error": "change_objective needs the objective's 'name'"}
    if estop_engaged():
        return {"error": f"e-stop engaged ({ESTOP}) - objective change refused"}
    if magnification(name) not in ALLOWED_MAGNIFICATIONS:
        allowed = "/".join(f"{m:g}x" for m in ALLOWED_MAGNIFICATIONS)
        return {"error": f"only {allowed} objectives are allowed, got {name!r}"}
    m = nis()
    with m.lock:
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


TOOLS = [
    ({"name": "echo",
      "description": "Return the text unchanged. Checks that the MCP server inside NIS-Elements answers.",
      "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
     echo),
    ({"name": "change_objective",
      "description": "Turn the nosepiece to an objective and switch NIS's calibration to it. Only the 4x "
                     "and 10x objectives are allowed. Give the objective's name exactly as on the "
                     "nosepiece; a wrong name returns the list.",
      "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
     change_objective),
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
    server cannot bind the same port on Windows."""
    http.server.ThreadingHTTPServer.allow_reuse_address = False
    srv = http.server.ThreadingHTTPServer((HOST, port), Handler)
    threading.Thread(target=srv.serve_forever, name="nis-mcp-server", daemon=True).start()
    return srv


def stop(srv: http.server.ThreadingHTTPServer) -> None:
    """Stop serving and free the port."""
    srv.shutdown()
    srv.server_close()


def stop_running(port: int = PORT) -> bool:
    """Ask a server already on the port to stop. True if one answered."""
    try:
        urllib.request.urlopen(urllib.request.Request(f"http://{HOST}:{port}/shutdown", method="POST"),
                               timeout=2).read()
        return True
    except OSError:
        return False


def run(imgs: "tuple[limjob.Image]", Job: "limjob.JobParam", macro: "limjob.MacroParam",
        ctx: "limjob.RunContext"):
    """JOBS entry point. Serving on the job's own thread (NIS's main thread)
    froze NIS, so serve on a daemon thread and return at once."""
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
    print(f"NIS MCP server {VERSION} on http://{HOST}:{srv.server_address[1]}/mcp")


if __name__ == "__main__":
    srv = start()
    print(f"NIS MCP server {VERSION} on http://{HOST}:{srv.server_address[1]}/mcp - Ctrl+C to stop")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        stop(srv)
