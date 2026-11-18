"""pytest test_nis_mcp.py  (the SDK test needs `pip install mcp`; it is skipped without it)"""
import base64
import json
import sqlite3
import struct
import threading
import time
import urllib.error
import urllib.request
import zlib

import pytest

import install_job as ij
import NISMCPServer as server


# --- server ------------------------------------------------------------
@pytest.fixture(autouse=True)
def no_real_log(monkeypatch, tmp_path):
    """Tests never write to the scope's own log."""
    monkeypatch.setattr(server, "LOG_FILE", tmp_path / "log.jsonl")
    monkeypatch.setattr(server, "_capture_job", None)
    native_init = server.NIS.__init__

    def mock_only_init(self, dll=None):
        if dll is None:
            raise AssertionError("Tests must never connect to real NIS")
        native_init(self, dll)

    monkeypatch.setattr(server.NIS, "__init__", mock_only_init)


@pytest.fixture
def url():
    srv = server.start(port=0)
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    server.stop(srv)


def post(url, body, headers=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def test_handshake_list_and_echo(url):
    status, out = post(url + "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                      "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                                 "clientInfo": {"name": "t", "version": "0"}}})
    assert status == 200 and out["result"]["protocolVersion"] == "2025-06-18"
    assert "Never answer from what an earlier call returned" in out["result"]["instructions"]
    assert post(url + "/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"})[0] == 202
    tools = post(url + "/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})[1]["result"]["tools"]
    assert [t["name"] for t in tools] == ["echo", "status", "move_relative", "focus_down", "change_objective",
                                          "list_optical_configs", "select_optical_config",
                                          "imaging_status", "capture", "capture_status",
                                          "list_experiments",
                                          "run_experiment",
                                          "finish_experiment", "estop"]
    out = post(url + "/mcp", {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                              "params": {"name": "echo", "arguments": {"text": "hello NIS"}}})[1]
    assert out["result"] == {"content": [{"type": "text", "text": "hello NIS"}], "isError": False}


def test_errors(url):
    assert post(url + "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "nope"})[1]["error"]["code"] == -32601
    out = post(url + "/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "nope"}})
    assert out[1]["error"]["code"] == -32602
    out = post(url + "/mcp", {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                              "params": {"name": "echo", "arguments": {}}})
    assert out[1]["result"]["isError"] is True
    unknown = post(url + "/mcp", {"jsonrpc": "2.0", "id": 4, "method": "initialize",
                                  "params": {"protocolVersion": "1999-01-01"}})
    assert unknown[1]["result"]["protocolVersion"] == server.PROTOCOL_VERSIONS[0]


def test_browser_pages_and_get_are_refused(url):
    assert post(url + "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                {"Origin": "http://evil.example"})[0] == 403
    assert post(url + "/shutdown", {}, {"Origin": "http://localhost:3000"})[0] == 404
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(url + "/mcp", timeout=5)
    assert e.value.code == 405


def test_rerun_replaces_the_running_server():
    srv = server.start(port=0)
    port = srv.server_address[1]
    assert server.stop_running(port)
    for _ in range(20):                                 # the port is freed just after the answer
        try:
            again = server.start(port=port)
            break
        except OSError:
            time.sleep(0.1)
    server.stop(again)
    assert not server.stop_running(port)


def test_reloads_on_save_and_keeps_serving_broken_code(tmp_path, monkeypatch):
    import importlib.util
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setenv("NIS_MCP_PORT", str(port))
    path = tmp_path / "NISMCPServer.py"
    source = open(server.__file__, encoding="utf-8").read().replace("WATCH_S = 1.0", "WATCH_S = 0.1")
    path.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("NISMCPServer", path)
    first = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(first)                      # what the job's loader does
    # No real Windows timer or NIS connection, even on the microscope PC.
    monkeypatch.setattr(first.MainThread, "install", lambda self: setattr(self, "installed", True))
    first.run(None, None, None, None)

    def version():
        return post(f"http://127.0.0.1:{port}/mcp", {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                                     "params": {}})[1]["result"]["serverInfo"]["version"]

    def wait_for(v):
        for _ in range(60):
            try:
                if version() == v:
                    return True
            except OSError:
                pass                                    # between the old server and the new
            time.sleep(0.1)
        return False

    try:
        assert version() == server.VERSION
        now = time.monotonic()
        first._capture_job = {"capture_id": "mock-running", "state": "running", "phase": "acquire",
                              "path": "mock.nd2", "timings_s": {}, "started_mono": now,
                              "phase_started_mono": now, "queue_deadline": now + 30}
        path.write_text(source.replace(f'"{server.VERSION}"', '"9.9.9"'), encoding="utf-8")
        time.sleep(0.8)
        assert version() == server.VERSION             # active capture ownership cannot be reloaded away
        first._capture_job = None
        assert wait_for("9.9.9")
        path.write_text(source + "\nthis is not python\n", encoding="utf-8")
        time.sleep(1.5)
        assert version() == "9.9.9"                     # broken code: the old server keeps serving
        path.write_text(source.replace(f'"{server.VERSION}"', '"9.9.10"'), encoding="utf-8")
        assert wait_for("9.9.10")                       # and the watcher still works after it
    finally:
        server.stop_running(port)


def test_real_mcp_client(url):
    anyio = pytest.importorskip("anyio")
    pytest.importorskip("mcp")
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async def go():
        async with streamable_http_client(url + "/mcp") as streams:
            async with ClientSession(streams[0], streams[1]) as s:
                await s.initialize()
                assert "echo" in [t.name for t in (await s.list_tools()).tools]
                return (await s.call_tool("echo", {"text": "via the SDK"})).content[0].text

    assert anyio.run(go) == "via the SDK"


# --- the tools, on a fake scope ----------------------------------------
class FakeScope:
    """The macro functions the tools use, with a stage and turret that obey."""

    def __init__(self):
        self.x, self.y, self.z = 100.0, 200.0, 4619.0
        self.pos, self.cal = 1, "Plan Fluor 4x"
        self.names = {0: "Plan Apo 20x", 1: "Plan Fluor 4x", 2: "Plan Apo 10x"}
        self.opt_confs = ["All Devices", "AX", "EPI"]
        self.nd_running = False
        self.nd_blocks = None               # an Event: ND_RunExperiment waits for it, as on the scope
        self.calls = []
        self.threads = {}                   # call -> id of the thread that made it

    def _record(self, *call):
        self.calls.append(call)
        self.threads[call[0]] = threading.get_ident()
        return 1

    def StgGetPosXY(self, px, py):
        px._obj.value, py._obj.value = self.x, self.y
        return 1

    def StgGetPosZ(self, pz, device):
        pz._obj.value = self.z
        return 1

    def StgMoveZ(self, dz, relative):
        assert relative == server.MOVE_RELATIVE
        self.z += dz
        return self._record("z", dz)

    def StgMoveXY(self, dx, dy, relative):
        assert relative == server.MOVE_RELATIVE
        self.x += dx
        self.y += dy
        return self._record("move", dx, dy)

    def GetOptConfCount(self):
        return len(self.opt_confs)

    def GetOptConfName(self, i, buf, n):
        buf.value = self.opt_confs[i] if 0 <= i < len(self.opt_confs) else ""
        return 1

    def Capture(self):
        return self._record("capture")

    def ImageSaveAs(self, path, kind, compression):
        assert kind == server.ND2_ALL_LAYERS
        with open(path, "wb") as f:
            f.write(b"mock ND2 data")
        return self._record("save", path)

    def CloseCurrentDocument(self, query):
        return self._record("close")

    def ND_LoadExperiment(self, name):
        self._record("nd_load", name)
        return 0                            # what the real one returned for a template that loads

    def ND_RunExperiment(self, open_after):
        self.nd_running = True
        self._record("nd_run")
        if self.nd_blocks:
            self.nd_blocks.wait(5)
        return 1

    def ND_IsInExperimentCapture(self):
        return int(self.nd_running)

    def ND_FinishExperiment(self):
        self.nd_running = False
        return self._record("nd_finish")

    def GetCurrentObjName(self, buf):
        buf.value = self.cal
        return 1

    def Stg_GetNosepiecePosition(self):
        return self.pos

    def Stg_GetNosepiecePositions(self):
        return len(self.names)

    def Stg_GetNosepieceObjectiveName(self, i, buf, n):
        if i not in self.names:
            return 0
        buf.value = self.names[i]
        return 1

    def Stg_SetNosepiecePosition(self, i):
        self.calls.append(("turn", i))
        self.pos = i
        return 1

    def ChangeObjective(self, name):
        self.calls.append(("calibrate", name))
        self.cal = name
        return 1

    # imaging state and the current image
    light_path, shutters = 2, {"EPI": 0, "DIA": 1}
    image = (40, 30, 12, 2)                 # width, height, bits, channels
    pixel = staticmethod(lambda r, c, k: 5 * k + r // 10)   # nearly blank, as the dark capture on 2026-11-05

    def GetSelectedOptConf(self, obj, cam, mic, n):
        obj.value, cam.value, mic.value = "", "Nikon AX:DIC_10x", "Nikon AX:DIC_10x"
        return 1

    def GetCurrentCameraName(self, name):
        name.value = "Mock camera"
        return 0

    def Stg_IsLightPathPresent(self):
        return 1

    def Stg_GetLightPathPosition(self):
        return self.light_path

    def Stg_GetLightPathPositions(self):
        return 4

    def Stg_GetShutterCount(self):
        return len(self.shutters)

    def Stg_GetShutterName(self, i, buf, n):
        buf.value = list(self.shutters)[i]
        return 1

    def Stg_GetShutterStateEx(self, name):
        return self.shutters[name]

    analyzer_state, pfs_status = 0, 0
    select_turns_to = None                  # a nosepiece position SelectOptConf also turns to

    def Stg_IsAnalyzerPresent(self):
        return 1

    def Stg_GetAnalyzerState(self):
        return self.analyzer_state

    def Stg_IsPFSPresent(self):
        return 1

    def Stg_GetPFSStatus(self):
        return self.pfs_status

    def SelectOptConf(self, name):
        if self.select_turns_to is not None:
            self.pos = self.select_turns_to
            self.cal = self.names[self.pos]
        return self._record("select", name)

    def Get_ImageInfo(self, name, w, h, bits, planes):
        assert name == ""                   # the current image
        if self.image is None:
            return -1
        w._obj.value, h._obj.value, bits._obj.value, planes._obj.value = self.image
        return 1

    def GetPixelValueEx(self, doc, undo, r, c, comp):
        assert (doc, undo) == (-1, 0) and 0 <= r < self.image[1] and 0 <= c < self.image[0]
        for k in range(self.image[3]):
            comp[k] = self.pixel(r, c, k)
        return 1


@pytest.fixture
def scope(monkeypatch, tmp_path):
    fake = FakeScope()
    monkeypatch.setattr(server, "_nis", server.NIS(fake))
    monkeypatch.setattr(server, "ESTOP", tmp_path / "ESTOP")
    monkeypatch.setattr(server, "ND_STORED", tmp_path / "ND_Stored.bin")
    monkeypatch.setattr(server, "LOG_FILE", tmp_path / "log.jsonl")
    monkeypatch.setattr(server, "XY_LIMITS_UM", ((-2000.0, 2000.0), (-2000.0, 2000.0)))
    write_nd_stored(server.ND_STORED, {n: experiment() for n in ("C. elegans 3h", "long", "e")})
    monkeypatch.setattr(server, "CAPTURE_DIR", tmp_path / "captures")
    monkeypatch.setattr(server, "_ui", server.MainThread())     # not installed, as off NIS
    return fake


def lv(name: str, value) -> bytes:
    """One record in NIS's LV format, as ND_Stored.bin holds them."""
    head = lambda t: bytes([t, len(name) + 1]) + (name + "\0").encode("utf-16-le")
    if isinstance(value, dict):
        body = b"".join(lv(k.split("#")[0], v) for k, v in value.items())
        h = head(11)
        return h + struct.pack("<IQ", len(value), len(h) + 12 + len(body)) + body + b"\0" * 8 * len(value)
    if isinstance(value, bool):
        return head(1) + struct.pack("<?", value)
    if isinstance(value, int):
        return head(3) + struct.pack("<I", value)
    if isinstance(value, float):
        return head(6) + struct.pack("<d", value)
    if isinstance(value, bytes):
        return head(9) + struct.pack("<Q", len(value)) + value
    return head(8) + (value + "\0").encode("utf-16-le")


def write_nd_stored(path, experiments: dict) -> None:
    raw = lv("StoredSettings", {n: {"SLxExperiment": {"NDAcquisition": a}} for n, a in experiments.items()})
    path.write_bytes(b"LV0Z" + struct.pack("<I", len(raw)) + b"\0" * 4 + zlib.compress(raw))


def experiment(time=(), z=None, xy=False, file=None) -> dict:
    """NDAcquisition settings as NIS 6.20 saves them, cut down to what list_experiments reads."""
    phases = {f"#{i}": {"eType": 0, "dInterval": iv, "dDuration": du, "uiCount": n}
              for i, (iv, du, n) in enumerate(time)}
    top = {"sName": "x", "bSaveToFile": True}
    if file:
        top.update(sPath=file[0], sFileName=file[1])
    return {"NDAcquisitionTop": top,
            "Tabs": {"bTimeChecked": bool(time), "bMultipointChecked": xy, "bZStackChecked": bool(z),
                     "bLambdaChecked": False, "bLargeImageChecked": False},
            "NDAcquisitionTimelapse": {"aData": phases},
            "NDAcquisitionMultipoint": {"NDAcquisitionListOfPoints": {"pData": b"\1\2"}},
            "NDAcquisitionZStack": {"ZStack": {"ZStack_Steps": z[0] if z else 0,
                                               "ZStack_Step": z[1] if z else 0.0}},
            "uiOrder": 0}


@pytest.fixture
def main_thread(monkeypatch):
    """Plays NIS's message loop: one thread calling _tick, as the Win32 timer does."""
    ui = server.MainThread()
    ui.installed, done = True, threading.Event()

    def loop():
        ui.main_thread_id = threading.get_ident()
        while not done.wait(0.01):
            ui._tick()

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    monkeypatch.setattr(server, "_ui", ui)
    yield ui
    done.set()
    t.join()


def tool(tool_name, **args):
    out = server.call_tool(tool_name, args)
    return json.loads(out["content"][0]["text"]), out["isError"]


def test_change_objective_turns_then_calibrates(scope):
    out, err = tool("change_objective", name="Plan Apo 10x")
    assert not err and out["objective"] == "Plan Apo 10x" and out["before"] == "Plan Fluor 4x"
    assert scope.calls == [("turn", 2), ("calibrate", "Plan Apo 10x")]
    assert tool("change_objective", name="Plan Apo 10x")[0] == {"objective": "Plan Apo 10x", "changed": False}


def test_change_objective_by_magnification(scope):
    out, err = tool("change_objective", name="10x")
    assert not err and out["objective"] == "Plan Apo 10x"
    assert scope.calls == [("turn", 2), ("calibrate", "Plan Apo 10x")]   # NIS gets the full name


def test_change_objective_by_magnification_refuses_ambiguity(scope):
    scope.names = {0: "Plan Apo 10x", 1: "Plan Fluor 4x", 2: "Plan Apo 10x DIC"}
    out, err = tool("change_objective", name="10x")
    assert err and "more than one" in out["error"] and scope.calls == []


def test_change_objective_refusals(scope):
    out, err = tool("change_objective", name="Plan Apo 20x")
    assert err and "only 4x/10x" in out["error"]
    out, err = tool("change_objective", name="Plan Apo 10X oil")
    assert err and out["nosepiece"] == {"0": "Plan Apo 20x", "1": "Plan Fluor 4x", "2": "Plan Apo 10x"}
    server.ESTOP.write_text("")
    out, err = tool("change_objective", name="Plan Apo 10x")
    assert err and "e-stop" in out["error"]
    assert scope.calls == []


def test_change_objective_reports_a_turret_that_does_not_arrive(scope, monkeypatch):
    monkeypatch.setattr(server, "TURRET_SETTLE_S", 0.3)
    scope.Stg_SetNosepiecePosition = lambda i: 1         # accepts, never moves
    out, err = tool("change_objective", name="Plan Apo 10x")
    assert err and out["rc_calibration"] is None and scope.cal == "Plan Fluor 4x"   # NIS not told


@pytest.mark.parametrize("phase", ["turret", "calibration"])
@pytest.mark.parametrize("delta", [-0.32, 0.25])
def test_change_objective_attributes_z_readback_change_without_restoring(scope, monkeypatch, phase, delta):
    method = "Stg_SetNosepiecePosition" if phase == "turret" else "ChangeObjective"
    original = getattr(scope, method)
    before = scope.z

    def native_change(*args):
        rc = original(*args)
        scope.z += delta
        return rc

    monkeypatch.setattr(scope, method, native_change)
    out, err = tool("change_objective", name="10x")
    assert not err and out["explicit_focus_command_issued"] is False
    assert out["change_completed"] and out["calibration_command_issued"]
    assert out["turret_position_changed"] and out["nosepiece_position_before"] == 1
    assert scope.calls == [("turn", 2), ("calibrate", "Plan Apo 10x")]
    assert scope.z == pytest.approx(before + delta)       # no corrective Z move
    z = out["z_readbacks"]
    assert list(z) == ["before_turret", "after_turret_before_calibration", "after_calibration"]
    assert all(s["rc"] == 1 and s["valid"] for s in z.values())
    assert z["before_turret"]["z_um"] == before
    assert z["after_turret_before_calibration"]["z_um"] == pytest.approx(
        before + delta if phase == "turret" else before)
    assert z["after_calibration"]["z_um"] == pytest.approx(before + delta)
    assert [s["monotonic_s"] for s in z.values()] == sorted(s["monotonic_s"] for s in z.values())
    assert out["z_deltas_um"] == {"turret": delta if phase == "turret" else 0,
                                   "calibration": delta if phase == "calibration" else 0,
                                   "total": delta}


@pytest.mark.parametrize("rc,value", [(-4, 2584.94), (1, float("nan")), (1, float("inf"))])
def test_change_objective_marks_failed_or_nonfinite_z_unknown(scope, monkeypatch, rc, value):
    def invalid_read(z, device):
        assert device == 0
        z._obj.value = value
        return rc

    monkeypatch.setattr(scope, "StgGetPosZ", invalid_read)
    out, err = tool("change_objective", name="10x")
    assert not err and not out["explicit_focus_command_issued"]
    assert all(s["z_um"] is None and s["rc"] == rc and not s["valid"]
               for s in out["z_readbacks"].values())
    assert out["z_deltas_um"] == {"turret": None, "calibration": None, "total": None}
    assert [c[0] for c in scope.calls] == ["turn", "calibrate"]


@pytest.mark.parametrize("stop_at_read", [1, 2])
def test_change_objective_rechecks_estop_after_z_read(scope, monkeypatch, stop_at_read):
    read = scope.StgGetPosZ
    reads = 0

    def stop_during_read(z, device):
        nonlocal reads
        rc = read(z, device)
        reads += 1
        if reads == stop_at_read:
            server.ESTOP.write_text("{}")
        return rc

    monkeypatch.setattr(scope, "StgGetPosZ", stop_during_read)
    out, err = tool("change_objective", name="10x")
    assert err and out["estop_engaged"] and out["rc_calibration"] is None
    assert scope.calls == ([] if stop_at_read == 1 else [("turn", 2)])
    assert not out["change_completed"] and not out["calibration_command_issued"]
    if stop_at_read == 2:
        assert out["objective"] is None and out["changed"] is None
        assert out["turret_position_changed"]
    assert len(out["z_readbacks"]) == stop_at_read
    assert "after_calibration" not in out["z_readbacks"]
    assert not server._nis.lock.locked()


def test_change_objective_rechecks_estop_during_turret_settle(scope, monkeypatch):
    # The native turn arrives normally; the stop appears during its polling
    # pause, so calibration would happen without the command-boundary check.
    monkeypatch.setattr(server.time, "sleep", lambda _: server.ESTOP.write_text("{}"))
    out, err = tool("change_objective", name="10x")
    assert err and out["estop_engaged"] and out["rc_calibration"] is None
    assert scope.calls == [("turn", 2)]
    assert out["nosepiece_position"] == 2
    assert out["objective"] is None and out["changed"] is None
    assert out["turret_position_changed"] and out["nosepiece_position_before"] == 1
    assert not out["change_completed"] and not out["calibration_command_issued"]
    assert scope.cal == "Plan Fluor 4x" and not server._nis.lock.locked()


def test_status(scope):
    out, err = tool("status")
    assert not err and out == {"xy_um": [100.0, 200.0], "z_um": 4619.0, "objective": "Plan Fluor 4x",
                               "nosepiece_position": 1, "nd_running": False, "estop_engaged": False,
                               "nosepiece": {"0": "Plan Apo 20x", "1": "Plan Fluor 4x", "2": "Plan Apo 10x"},
                               "xy_limits_um": [[-2000.0, 2000.0], [-2000.0, 2000.0]],
                               "z_devices": {"0": {"z_um": 4619.0, "rc": 1}, "1": {"z_um": 4619.0, "rc": 1}}}


def test_move_relative_and_its_refusals(scope):
    out, err = tool("move_relative", dx_um=50, dy_um=-20)
    assert not err and out["before_um"] == [100.0, 200.0] and out["after_um"] == [150.0, 180.0]
    for args in ({"dx_um": 1001, "dy_um": 0}, {"dx_um": float("nan"), "dy_um": 0}, {"dx_um": "far"}):
        assert tool("move_relative", **args)[1]
    server.ESTOP.write_text("")
    out, err = tool("move_relative", dx_um=1, dy_um=0)
    assert err and out["estop_engaged"]
    assert scope.calls == [("move", 50.0, -20.0)]


def test_move_relative_stays_in_the_safe_area(scope, monkeypatch):
    for _ in range(2):                                   # 100 -> 1100 -> 2100: the second step leaves
        out, err = tool("move_relative", dx_um=1000, dy_um=0)
    assert err and "outside the safe XY area" in out["error"] and out["position_um"] == [1100.0, 200.0]
    assert not tool("move_relative", dx_um=900, dy_um=-1000)[1]          # to the corner (2000, -800)
    assert tool("move_relative", dx_um=0, dy_um=-1500)[1]                # y -2300: out
    assert scope.calls == [("move", 1000.0, 0.0), ("move", 900.0, -1000.0)]
    monkeypatch.setattr(server, "XY_LIMITS_UM", None)
    out, err = tool("move_relative", dx_um=1, dy_um=0)
    assert err and "no safe XY area" in out["error"] and len(scope.calls) == 2


def test_every_call_is_logged(scope):
    tool("move_relative", dx_um=50, dy_um=0)
    server.ESTOP.write_text("")
    tool("capture", filename="x")
    lines = [json.loads(s) for s in server.LOG_FILE.read_text(encoding="utf-8").splitlines()]
    assert [(e["tool"], e["ok"]) for e in lines] == [("move_relative", True), ("capture", False)]
    assert lines[0]["args"] == {"dx_um": 50, "dy_um": 0} and lines[0]["result"]["after_um"] == [150.0, 200.0]
    assert "e-stop" in lines[1]["result"]["error"] and lines[1]["time"]


def test_a_log_that_cannot_be_written_does_not_stop_a_tool(scope, monkeypatch):
    monkeypatch.setattr(server, "LOG_FILE", server.ND_STORED)       # an existing file...
    server.LOG_FILE.unlink()
    server.LOG_FILE.mkdir()                                          # ...replaced by a folder
    out, err = tool("move_relative", dx_um=5, dy_um=0)
    assert not err and out["after_um"] == [105.0, 200.0]


def test_list_optical_configs(scope):
    assert tool("list_optical_configs") == ({"optical_configs": ["All Devices", "AX", "EPI"]}, False)


def test_select_optical_config_runs_on_the_main_thread(scope, main_thread):
    out, err = tool("select_optical_config", name="AX")
    assert not err and out["optical_config"] == "AX" and out["rc_select"] == 1
    assert out["objective_before"] == out["objective"] == "Plan Fluor 4x"
    assert out["imaging"]["analyzer"] == {"state": "out"}
    assert out["imaging"]["pfs"] == {"status": 0, "meaning": "off, in range"}
    assert scope.calls == [("select", "AX")]
    assert scope.threads["select"] == main_thread.main_thread_id


def test_select_optical_config_refusals(scope, main_thread):
    scope.opt_confs = ["AX", "Nikon AX:DIC_20x", "Eye Piece:DIC60x"]
    assert "needs the configuration" in tool("select_optical_config")[0]["error"]
    out, err = tool("select_optical_config", name="nope")
    assert err and out["optical_configs"] == scope.opt_confs
    for name in ("Nikon AX:DIC_20x", "Eye Piece:DIC60x"):
        out, err = tool("select_optical_config", name=name)
        assert err and "objective" in out["error"], name
    server.ESTOP.write_text("{}")
    out, err = tool("select_optical_config", name="AX")
    assert err and out["estop_engaged"]
    assert scope.calls == []


def test_select_optical_config_needs_the_main_thread(scope):
    out, err = tool("select_optical_config", name="AX")
    assert err and "main thread" in out["error"] and scope.calls == []


def test_select_optical_config_engages_estop_on_a_disallowed_objective(scope, main_thread):
    scope.select_turns_to = 0               # the configuration turns to the 20x
    out, err = tool("select_optical_config", name="AX")
    assert err and out["estop_engaged"] and out["objective"] == "Plan Apo 20x"
    assert server.estop_engaged()


def test_imaging_status_reports_analyzer_and_pfs(scope):
    scope.analyzer_state, scope.pfs_status = 1, 1
    out, err = tool("imaging_status")
    assert not err and out["analyzer"] == {"state": "in"}
    assert out["pfs"] == {"status": 1, "meaning": "on, focused"}


def test_capture_runs_on_the_main_thread(scope, main_thread):
    out, err = tool("capture", filename="aml18_check")
    path = server.CAPTURE_DIR / "aml18_check.nd2"
    assert not err and out["path"] == str(path) and out["saved"] and path.exists()
    assert [c[0] for c in scope.calls] == ["capture", "save", "close"]
    assert scope.threads["capture"] == main_thread.main_thread_id
    out, err = tool("capture", filename="aml18_check.nd2")
    assert err and "already exists" in out["error"]
    for bad in ("../up", "D:\\data\\x", "", ".hidden"):
        assert tool("capture", filename=bad)[1], bad
    assert len(scope.calls) == 3


def test_imaging_status(scope, monkeypatch):
    monkeypatch.setattr(server, "LIGHT_PATH_NAMES", {2: "L100"})
    out, err = tool("imaging_status")
    assert not err
    assert out["optical_config"]["camera"] == "Nikon AX:DIC_10x"
    assert out["optical_config_rc"] == 1
    assert out["objective"] == "Plan Fluor 4x"
    assert out["camera_name"] == "Mock camera" and out["camera_name_rc"] == 0
    assert "diagnostic_errors" not in out
    assert out["light_path"] == {"port": 2, "port_name": "L100", "ports": 4}
    assert out["shutters"] == {"EPI": "closed", "DIA": "open"} and out["estop_engaged"] is False
    assert scope.calls == []  # no acquisition, device selection, or movement


@pytest.mark.parametrize("config_rc", [0, 1, -4])
def test_imaging_diagnostics_separate_hardware_names_from_blank_configs(scope, config_rc):
    def blank_configs(obj, cam, mic, n):
        return config_rc

    scope.GetSelectedOptConf = blank_configs
    out, err = tool("imaging_status")
    assert not err and out["optical_config"] == {"objective": "", "camera": "", "microscope": ""}
    assert out["optical_config_rc"] == config_rc  # no invented return-code interpretation
    assert out["objective"] == "Plan Fluor 4x" and out["camera_name"] == "Mock camera"
    assert scope.calls == []


def test_imaging_diagnostics_empty_camera_is_unknown_not_disconnected(scope):
    scope.GetCurrentCameraName = lambda name: -4
    out, err = tool("imaging_status")
    assert not err and out["camera_name"] is None and out["camera_name_rc"] == -4
    assert out["objective"] == "Plan Fluor 4x" and scope.calls == []


@pytest.mark.parametrize("function", ["GetSelectedOptConf", "GetCurrentObjName", "GetCurrentCameraName"])
def test_imaging_diagnostic_read_error_keeps_other_readbacks(scope, function):
    def unavailable(*args):
        raise AttributeError("mock diagnostic unavailable")

    setattr(scope, function, unavailable)
    out, err = tool("imaging_status")
    assert not err and out["diagnostic_errors"] == {function: "AttributeError: mock diagnostic unavailable"}
    assert out["shutters"] == {"EPI": "closed", "DIA": "open"}
    assert out["light_path"]["port"] == 2 and scope.calls == []
    if function == "GetSelectedOptConf":
        assert out["optical_config_rc"] is None and out["camera_name"] == "Mock camera"
    elif function == "GetCurrentObjName":
        assert out["objective"] is None and out["objective_rc"] is None
        assert out["camera_name"] == "Mock camera"
    else:
        assert out["camera_name"] is None and out["camera_name_rc"] is None
        assert out["objective"] == "Plan Fluor 4x"


def test_camera_name_uses_new_ml_export_and_caches_binding(scope, monkeypatch):
    resolved, loaded, calls = [], [], []

    class Getter:
        def __call__(self, name):
            calls.append("camera name")
            name.value = "Mock selected camera from new_ml"
            return 0

    getter = Getter()

    class CameraDLL:
        def __getattr__(self, name):
            resolved.append(name)
            assert name == "?GetCurrentCameraName@@YAHPEA_W@Z"
            return getter

    class MockLoader:
        def __getattr__(self, name):
            loaded.append(name)
            assert name == "g5_new_ml"  # no regprocs lookup or unverified _Impl call
            return CameraDLL()

    # Exercise the production resolution branch with an entirely fake loader.
    # NIS itself was constructed with FakeScope; no native library is loaded.
    monkeypatch.setattr(server.ct, "cdll", MockLoader())
    server._nis._native = True
    assert loaded == []  # optional binding is lazy
    for _ in range(2):
        out, err = tool("imaging_status")
        assert not err and out["camera_name"] == "Mock selected camera from new_ml"
        assert out["camera_name_rc"] == 0 and "diagnostic_errors" not in out
        assert out["objective"] == "Plan Fluor 4x"
    assert loaded == ["g5_new_ml"] and resolved == [server.NIS.CAMERA_NAME_EXPORT]
    assert getter.argtypes == [server.ct.c_wchar_p] and getter.restype is server.ct.c_int
    assert calls == ["camera name", "camera name"] and scope.calls == []


@pytest.mark.parametrize("missing", ["dll", "export"])
def test_camera_name_new_ml_unavailable_keeps_other_diagnostics(scope, monkeypatch, missing):
    class MissingExport:
        def __getattr__(self, name):
            assert name == server.NIS.CAMERA_NAME_EXPORT
            raise AttributeError("mock camera-name export missing")

    class MockLoader:
        def __getattr__(self, name):
            assert name == "g5_new_ml"
            if missing == "dll":
                raise OSError("mock camera DLL unavailable")
            return MissingExport()

    monkeypatch.setattr(server.ct, "cdll", MockLoader())
    server._nis._native = True
    out, err = tool("imaging_status")
    assert not err and out["camera_name"] is None and out["camera_name_rc"] is None
    expected = ("OSError: mock camera DLL unavailable" if missing == "dll" else
                "AttributeError: mock camera-name export missing")
    assert out["diagnostic_errors"] == {"GetCurrentCameraName": expected}
    assert out["objective"] == "Plan Fluor 4x" and out["optical_config_rc"] == 1
    assert out["light_path"]["port"] == 2 and out["shutters"] == {"EPI": "closed", "DIA": "open"}
    assert scope.calls == []


def test_capture_reports_intensity_and_warns_on_low_signal(scope, main_thread):
    out, err = tool("capture", filename="stats", include_stats=True)
    assert not err and out["saved"]
    img = out["image"]
    assert (img["width"], img["height"], img["bits"], img["full_scale"]) == (40, 30, 12, 4095)
    assert img["sampled_pixels"] == 40 * 30                 # smaller than the grid: every pixel
    ch0, ch1 = img["channels"]
    assert (ch0["min"], ch0["max"], ch1["min"], ch1["max"]) == (0, 2, 5, 7)
    assert ch0["saturated_fraction"] == 0
    assert out["imaging"]["light_path"]["port"] == 2
    assert len(out["warnings"]) == 1 and "low signal" in out["warnings"][0]     # 7 < 1% of 4095
    assert [c[0] for c in scope.calls] == ["capture", "save", "close"]


def test_capture_bright_and_saturated(scope, main_thread):
    scope.pixel = lambda r, c, k: 4095 if r < 3 else 2000                  # 10% saturated
    out, err = tool("capture", filename="bright", include_stats=True)
    assert not err and out["image"]["channels"][0]["saturated_fraction"] == 0.1
    assert all("saturated" in w for w in out["warnings"]) and len(out["warnings"]) == 2


def test_capture_samples_a_grid_of_a_large_image(scope, main_thread, monkeypatch):
    monkeypatch.setattr(server, "IMAGE_SAMPLE_GRID", 16)
    scope.image = (1024, 512, 16, 1)
    scope.pixel = lambda r, c, k: 30000
    out, err = tool("capture", filename="big", include_stats=True)
    assert not err and "warnings" not in out
    assert out["image"]["sampled_pixels"] == 16 * 16 and out["image"]["channels"][0]["p50"] == 30000


def test_capture_keeps_the_file_when_the_image_cannot_be_read(scope, main_thread):
    scope.image = None
    out, err = tool("capture", filename="noinfo", include_stats=True)
    assert not err and out["saved"] and "error" in out["image"] and "warnings" not in out
    assert [c[0] for c in scope.calls] == ["capture", "save", "close"]


def test_capture_and_nd_refused_without_the_main_thread_hook(scope):
    for name, args in (("capture", {"filename": "x"}), ("run_experiment", {"experiment": "e"}),
                       ("finish_experiment", {})):
        out, err = tool(name, **args)
        assert err and "main thread" in out["error"], name
    assert scope.calls == []


def wait_capture(capture_id):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        out, err = tool("capture_status", capture_id=capture_id)
        assert not err
        if not out["capture_in_progress"]:
            return out
        time.sleep(0.005)
    raise AssertionError("mock capture did not finish")


def test_capture_default_skips_native_pixel_reads_and_reports_phase_timings(scope, main_thread):
    scope.GetPixelValueEx = lambda *args: pytest.fail("default capture must skip pixel reads")
    out, err = tool("capture", filename="fast")
    assert not err and out["state"] == "completed" and out["statistics_skipped"]
    assert set(out["timings_s"]) == {"queue_wait", "wait_nis", "read_imaging", "acquire", "save", "close"}
    assert all(v >= 0 for v in out["timings_s"].values())
    progress = tool("capture_status", capture_id=out["capture_id"])[0]
    assert not progress["capture_in_progress"] and progress["result"] == out
    entries = [json.loads(line) for line in server.LOG_FILE.read_text().splitlines()]
    phases = [e["phase"] for e in entries if e.get("event") == "capture_phase_started"]
    assert phases == ["wait_nis", "read_imaging", "acquire", "save", "close"]
    assert sum(e.get("event") == "capture_phase_finished" for e in entries) == 5
    assert entries[0]["event"] == "capture_queued"
    assert any(e.get("event") == "capture_finished" and e["state"] == "completed" for e in entries)


def test_slow_capture_returns_progress_keeps_lock_and_blocks_retries(scope, main_thread, monkeypatch):
    monkeypatch.setattr(server, "CAPTURE_RESPONSE_WAIT_S", 0.02)
    started, release = threading.Event(), threading.Event()

    def slow_capture():
        scope._record("capture")
        started.set()
        assert release.wait(2)
        return 0  # observed NIS success code: do not mistake zero for failure

    scope.Capture = slow_capture
    try:
        out, err = tool("capture", filename="slow")
        assert not err and started.wait(1) and out["capture_in_progress"]
        cid = out["capture_id"]
        t0 = time.monotonic()
        progress = tool("capture_status", capture_id=cid)[0]
        assert time.monotonic() - t0 < 0.2 and progress["phase"] == "acquire"
        assert server._nis.lock.locked()  # HTTP returned, native operation still owns NIS
        assert tool("status")[0]["capture_in_progress"]
        assert tool("imaging_status")[0]["capture_in_progress"]
        assert server.call_tool("echo", {"text": "responsive"})["content"][0]["text"] == "responsive"
        assert tool("capture", filename="duplicate")[1]
        assert tool("move_relative", dx_um=1, dy_um=0)[1]
        assert tool("change_objective", name="Plan Apo 10x")[1]
        assert tool("run_experiment", experiment="e")[1]
        assert tool("finish_experiment")[1]
        assert tool("estop", action="engage")[0]["engaged"]
        assert scope.calls == [("capture",)]
    finally:
        release.set()
    done = wait_capture(cid)
    assert done["state"] == "completed" and done["result"]["saved"]
    assert not server._nis.lock.locked()
    assert server.ESTOP.exists()  # completing the image never releases E-stop
    assert tool("capture", filename="still_stopped")[1]
    assert [x[0] for x in scope.calls] == ["capture", "save", "close"]


def test_capture_queue_expires_without_late_acquisition(scope, monkeypatch):
    ui = server.MainThread()
    ui.installed = True  # deliberately no fake message pump yet
    monkeypatch.setattr(server, "_ui", ui)
    monkeypatch.setattr(server, "CAPTURE_RESPONSE_WAIT_S", 0.001)
    out, err = tool("capture", filename="stale")
    assert not err and out["state"] == "queued"
    server._capture_job["queue_deadline"] = time.monotonic() - 1
    expired = tool("capture_status", capture_id=out["capture_id"])[0]
    assert expired["state"] == "cancelled" and not expired["capture_in_progress"]
    ui._tick()
    assert scope.calls == []


def test_slow_capture_progress_over_mock_http(url, scope, main_thread, monkeypatch):
    monkeypatch.setattr(server, "CAPTURE_RESPONSE_WAIT_S", 0.01)
    release, started = threading.Event(), threading.Event()

    def blocked():
        scope._record("capture")
        started.set()
        assert release.wait(3)
        return 0

    scope.Capture = blocked

    def rpc(name, args):
        status_code, response = post(url + "/mcp", {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": args}})
        assert status_code == 200 and not response["result"]["isError"]
        return json.loads(response["result"]["content"][0]["text"])

    try:
        result = rpc("capture", {"filename": "http_slow"})
        assert result["capture_in_progress"]
        assert started.wait(1)
        progress = rpc("capture_status", {"capture_id": result["capture_id"]})
        assert progress["capture_in_progress"] and progress["phase"] == "acquire"
    finally:
        release.set()
    assert wait_capture(result["capture_id"])["result"]["saved"]


def test_estop_is_rechecked_before_a_queued_capture_starts(scope, monkeypatch):
    ui = server.MainThread()
    ui.installed = True
    monkeypatch.setattr(server, "_ui", ui)
    monkeypatch.setattr(server, "CAPTURE_RESPONSE_WAIT_S", 0.001)
    out, err = tool("capture", filename="queued_stop")
    assert not err
    tool("estop", action="engage")
    ui._tick()
    done = tool("capture_status", capture_id=out["capture_id"])[0]
    assert done["state"] == "cancelled" and scope.calls == []


def test_capture_failure_logs_exact_phase_and_does_not_save_or_close(scope, main_thread):
    def broken_capture():
        scope._record("capture")
        raise RuntimeError("mock device error")
    scope.Capture = broken_capture
    out, err = tool("capture", filename="broken")
    assert err and out["state"] == "failed" and "mock device error" in out["error"]
    assert out["failed_phase"] == "acquire"
    assert [x[0] for x in scope.calls] == ["capture"]
    entries = [json.loads(line) for line in server.LOG_FILE.read_text().splitlines()]
    assert any(e.get("event") == "capture_phase_error" and e["phase"] == "acquire" for e in entries)
    assert tool("capture_status")[0]["result"] == out


@pytest.mark.parametrize("stop_during", ["imaging_read", "acquire_phase_start"])
def test_capture_rechecks_estop_immediately_before_acquisition(scope, main_thread, monkeypatch, stop_during):
    if stop_during == "imaging_read":
        original = server.NIS.imaging

        def imaging_then_stop(self):
            result = original(self)
            server.estop({"action": "engage", "reason": "mock pre-acquisition stop"})
            return result

        monkeypatch.setattr(server.NIS, "imaging", imaging_then_stop)
    else:
        original = server._capture_event

        def event_then_stop(job, event, **fields):
            original(job, event, **fields)
            if event == "capture_phase_started" and fields.get("phase") == "acquire":
                server.estop({"action": "engage", "reason": "mock acquisition-phase stop"})

        monkeypatch.setattr(server, "_capture_event", event_then_stop)

    out, err = tool("capture", filename="stopped_before_acquire")
    assert err and out["state"] == "cancelled" and out["estop_engaged"]
    assert not out["saved"] and "e-stop" in out["error"]
    assert scope.calls == []  # no Capture, save, close, or movement
    assert not server.Path(out["path"]).exists()
    assert not server._nis.lock.locked() and server.ESTOP.exists()
    progress, query_error = tool("capture_status", capture_id=out["capture_id"])
    assert not query_error and not progress["capture_in_progress"]
    assert progress["state"] == "cancelled" and progress["result"] == out
    entries = [json.loads(line) for line in server.LOG_FILE.read_text().splitlines()]
    terminal = [entry for entry in entries if entry.get("event") == "capture_finished"]
    assert len(terminal) == 1 and terminal[0]["state"] == "cancelled"
    acquire_finished = [entry for entry in entries if entry.get("event") == "capture_phase_finished"
                        and entry.get("phase") == "acquire"]
    assert len(acquire_finished) == 1
    assert acquire_finished[0]["took_s"] == out["timings_s"]["acquire"]
    assert not any(entry.get("phase") in ("save", "statistics", "close") for entry in entries)


@pytest.mark.parametrize("empty_file", [False, True])
def test_failed_or_empty_save_leaves_document_open(scope, main_thread, empty_file):
    def no_image_saved(path, *args):
        if empty_file:
            server.Path(path).touch()
        return 0
    scope.ImageSaveAs = no_image_saved
    out, err = tool("capture", filename="not_saved")
    assert err and not out["saved"] and "left open" in out["error"]
    assert [x[0] for x in scope.calls] == ["capture"]
    assert "save" in out["timings_s"] and "close" not in out["timings_s"]


def test_capture_status_rejects_wrong_id_without_entering_nis(scope):
    assert tool("capture_status")[0]["state"] == "idle"
    assert tool("capture_status", capture_id="not-an-operation")[1]
    assert scope.calls == []


def test_capture_rejects_invalid_statistics_option(scope, main_thread):
    assert tool("capture", filename="bad_option", include_stats="yes")[1]
    assert scope.calls == []


def test_run_and_finish_experiment(scope, main_thread, monkeypatch):
    out, err = tool("run_experiment", experiment="C. elegans 3h")
    assert not err and out["nd_running"] and out["rc_load"] == 0
    assert scope.threads["nd_run"] == main_thread.main_thread_id
    out, err = tool("run_experiment", experiment="C. elegans 3h")
    assert err and "already running" in out["error"]
    server.ESTOP.write_text("")                     # stopping is allowed through the e-stop
    assert tool("finish_experiment") == ({"rc": 1, "nd_running": False}, False)
    assert "e-stop" in tool("run_experiment", experiment="C. elegans 3h")[0]["error"]
    server.ESTOP.unlink()
    monkeypatch.setattr(server, "ALLOWED_EXPERIMENTS", ("Embryo 1h",))
    out, err = tool("run_experiment", experiment="C. elegans 3h")
    assert err and "ALLOWED_EXPERIMENTS" in out["error"]


def test_run_experiment_refuses_a_name_that_is_not_saved(scope, main_thread):
    out, err = tool("run_experiment", experiment="C. elegans 3H")
    assert err and "no saved ND experiment" in out["error"] and out["saved"] == ["C. elegans 3h", "long", "e"]
    server.ND_STORED.write_bytes(b"LV0Z garbage")
    out, err = tool("run_experiment", experiment="C. elegans 3h")
    assert err and "cannot check the name" in out["error"]
    server.ND_STORED.unlink()
    assert "cannot check the name" in tool("run_experiment", experiment="C. elegans 3h")[0]["error"]
    assert scope.calls == []


def test_run_experiment_refuses_existing_explicit_output_before_load(scope, main_thread, tmp_path):
    output = tmp_path / "existing.nd2"
    original = b"existing acquisition must be preserved"
    output.write_bytes(original)
    write_nd_stored(server.ND_STORED, {"collision": experiment(file=(str(tmp_path), output.name))})
    out, err = tool("run_experiment", experiment="collision")
    assert err and "output already exists" in out["error"]
    assert out["output_path"] == str(output) and output.read_bytes() == original
    assert scope.calls == [] and server._nd_run is None


def test_run_experiment_rechecks_output_after_native_load(scope, main_thread, tmp_path, monkeypatch):
    output = tmp_path / "appeared.nd2"
    write_nd_stored(server.ND_STORED, {"collision": experiment(file=(str(tmp_path), output.name))})
    load = scope.ND_LoadExperiment

    def load_then_other_writer(name):
        rc = load(name)
        output.write_bytes(b"appeared during native loading")
        return rc

    monkeypatch.setattr(scope, "ND_LoadExperiment", load_then_other_writer)
    out, err = tool("run_experiment", experiment="collision")
    assert err and "output already exists" in out["error"] and out["rc_load"] == 0
    assert out["output_path"] == str(output)
    assert output.read_bytes() == b"appeared during native loading"
    assert scope.calls == [("nd_load", "collision")] and server._nd_run is None
    assert not server._nis.lock.locked()


def test_run_experiment_starts_when_explicit_output_is_absent(scope, main_thread, tmp_path):
    output = tmp_path / "new.nd2"
    write_nd_stored(server.ND_STORED, {"new": experiment(file=(str(tmp_path), output.name))})
    out, err = tool("run_experiment", experiment="new")
    assert not err and out["rc_load"] == 0
    assert scope.calls == [("nd_load", "new"), ("nd_run",)]
    assert not output.exists()                          # fake ND acquisition writes no raw data


def test_run_experiment_rechecks_estop_after_native_load(scope, main_thread, monkeypatch):
    load = scope.ND_LoadExperiment

    def load_then_stop(name):
        rc = load(name)
        server.ESTOP.write_text("{}")
        return rc

    monkeypatch.setattr(scope, "ND_LoadExperiment", load_then_stop)
    out, err = tool("run_experiment", experiment="e")
    assert err and out["estop_engaged"]
    assert scope.calls == [("nd_load", "e")] and server._nd_run is None


def test_run_experiment_refuses_a_long_or_unclear_time_lapse(scope, main_thread):
    write_nd_stored(server.ND_STORED, {
        "90 s": experiment(time=[(20_000.0, 90_000.0, 5)]),
        "by frames": experiment(time=[(60_000.0, 0.0, 21)]),            # 20 min from interval x frames
        "two phases": experiment(time=[(60_000.0, 1_200_000.0, 0), (60_000.0, 1_200_000.0, 0)]),
        "overnight": experiment(time=[(60_000.0, 27_000_000.0, 451)]),
        "unclear": experiment(time=[(0.0, 0.0, 0)]),
        "stack": experiment(z=(41, 0.75))})
    out, err = tool("run_experiment", experiment="two phases")
    assert err and "40 min" in out["error"] and out["time_lapse_s"] == 2400
    out, err = tool("run_experiment", experiment="overnight")
    assert err and "450 min" in out["error"] and "MAX_ND_RUN_S" in out["error"]
    assert "cannot be worked out" in tool("run_experiment", experiment="unclear")[0]["error"]
    assert scope.calls == []
    for ok in ("90 s", "by frames", "stack"):
        out, err = tool("run_experiment", experiment=ok)
        assert not err, ok
        scope.nd_running = False


def test_list_experiments(scope):
    write_nd_stored(server.ND_STORED, {
        "Pilot 60s": experiment(time=[(60_000.0, 3_600_000.0, 61)], z=(1, 0.75), file=("D:\\data", "p.nd2")),
        "Stack": experiment(z=(41, 0.75)),
        "Grid": experiment(xy=True)})
    out, err = tool("list_experiments")
    assert not err and out["file"] == str(server.ND_STORED) and out["file_written"]
    assert out["experiments"] == [
        {"name": "Pilot 60s", "loops": ["time", "z_stack"], "time_lapse_s": 3600.0,
         "z_stack": {"steps": 1, "step_um": 0.75},
         "time": [{"interval_s": 60.0, "duration_s": 3600.0, "frames": 61}],
         "saves_to": str(server.Path("D:\\data") / "p.nd2")},
        {"name": "Stack", "loops": ["z_stack"], "time_lapse_s": None, "single_pass": True, "note": server._length_fields(0)["note"], "z_stack": {"steps": 41, "step_um": 0.75}},
        {"name": "Grid", "loops": ["xy_points"], "time_lapse_s": None, "single_pass": True, "note": server._length_fields(0)["note"]}]
    assert scope.calls == []


@pytest.mark.skipif(not server.ND_STORED.exists(), reason="no NIS on this PC")
def test_reads_this_pcs_saved_experiments():
    saved = server.saved_experiments()       # raises if a NIS update changed the format
    assert saved and all("Tabs" in a for a in saved.values())


def test_list_experiments_refuses_a_file_it_does_not_recognise(scope):
    good = server.ND_STORED.read_bytes()
    raw = zlib.decompress(good[12:])
    bad = {"not LV0Z": b"XXXX" + good[4:],
           "wrong length": good[:4] + struct.pack("<I", len(raw) + 1) + good[8:],
           "unknown type": good[:12] + zlib.compress(b"\x63" + raw[1:]),
           "truncated": good[:4] + struct.pack("<I", len(raw) - 3) + good[8:12] + zlib.compress(raw[:-3])}
    for why, data in bad.items():
        server.ND_STORED.write_bytes(data)
        out, err = tool("list_experiments")
        assert err and "cannot read" in out["error"], why


def test_other_tools_answer_busy_while_an_nd_run_blocks(scope, main_thread, monkeypatch):
    monkeypatch.setattr(server, "BUSY_WAIT_S", 0.2)
    scope.nd_blocks = threading.Event()
    run = threading.Thread(target=tool, args=("run_experiment",), kwargs={"experiment": "long"})
    run.start()
    while "nd_run" not in scope.threads:
        time.sleep(0.01)
    t0 = time.time()
    out, err = tool("status")                       # answers from the server's record, at once
    assert not err and time.time() - t0 < 0.1 and out["nd_running"] and "Finish button" in out["note"]
    assert out["nd_run"]["experiment"] == "long" and out["nd_run"]["xy_um_at_start"] == [100.0, 200.0]
    out, err = tool("move_relative", dx_um=1, dy_um=0)
    assert err and "NISBusy" in out["error"]
    assert tool("estop", action="engage")[0]["engaged"]      # stopping still works mid-run
    scope.nd_blocks.set()
    run.join()
    out, err = tool("status")
    assert not err and "nd_run" not in out and out["xy_um"] == [100.0, 200.0]


def test_status_reports_when_an_nd_run_should_end(scope, main_thread):
    write_nd_stored(server.ND_STORED, {"90 s": experiment(time=[(20_000.0, 90_000.0, 5)])})
    scope.nd_blocks = threading.Event()
    run = threading.Thread(target=tool, args=("run_experiment",), kwargs={"experiment": "90 s"})
    run.start()
    while "nd_run" not in scope.threads:
        time.sleep(0.01)
    r = tool("status")[0]["nd_run"]
    scope.nd_blocks.set()
    run.join()
    started, end = (server.datetime.datetime.fromisoformat(r[k]) for k in ("started", "expected_end"))
    assert (end - started).total_seconds() == 90 and r["time_lapse_s"] == 90


def test_estop_engages_and_cannot_release(scope):
    assert tool("estop", action="status") == ({"engaged": False, "flag_file": str(server.ESTOP)}, False)
    out, err = tool("estop", action="engage", reason="drifting")
    assert not err and out["engaged"] and out["detail"]["reason"] == "drifting"
    assert tool("change_objective", name="Plan Apo 10x")[1]
    out, err = tool("estop", action="release")
    assert err and server.ESTOP.exists()


# --- installer ---------------------------------------------------------
def python_task(script: str) -> str:
    items = [(11, "PythonScript\0", ([(8, "Script\0", script), (8, "ScriptError\0", "")], [1, 0]))]
    return base64.b64encode(ij.serialize(items)).decode()


def test_codec_round_trips():
    real = "CwtQAGEAcgBhAG0AZQB0AGUAcgBzAAAAAAAAACQAAAAAAAAA"    # an empty level from NIS
    assert base64.b64encode(ij.serialize(ij.parse(base64.b64decode(real))[0])).decode() == real
    blob = python_task("print('old')")
    assert ij.set_script(blob, "print('old')") == blob
    assert ij.get_script(ij.set_script(blob, "x = 1\n" * 1000)) == "x = 1\n" * 1000


@pytest.fixture
def jobs_db(tmp_path):
    path = tmp_path / "Jobs.dat"
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE jobdef (iKey INTEGER PRIMARY KEY AUTOINCREMENT, iProjKeyRef INTEGER, iDefType INTEGER,
            iState INTEGER, iProgress INTEGER, dCTime REAL, dMTime REAL, wszUuid TEXT, wszName TEXT,
            wszDesc TEXT, pUsability BLOB, wszJsonAppKeyValues TEXT);
        CREATE TABLE jobprogram (iKey INTEGER PRIMARY KEY AUTOINCREMENT, iJobdefKeyRef INTEGER,
            wszSummary TEXT, pProperties BLOB, wszUuid TEXT, dMTime REAL, wszJsonTaskList TEXT);
        INSERT INTO jobdef VALUES (1, 1, 0, 268435456, 0, 2461000, 2461000, 'aa', 'OldPython', '', NULL, '{}');
    """)
    tasks = [{"block": 1, "class": "PythonScript", "data": python_task("print('old')"), "name": "OldPython"}]
    db.execute("INSERT INTO jobprogram VALUES (1, 1, '', NULL, 'bb', 2461000, ?)", (json.dumps(tasks),))
    db.commit()
    db.close()
    return path


def job(path, name):
    db = sqlite3.connect(path)
    key = db.execute("SELECT iKey FROM jobdef WHERE wszName=?", (name,)).fetchone()[0]
    n = db.execute("SELECT count(*) FROM jobprogram WHERE iJobdefKeyRef=?", (key,)).fetchone()[0]
    task = json.loads(ij.latest_program(db, key)[1])[0]
    db.close()
    return key, n, task


def test_install_creates_then_updates(jobs_db, tmp_path):
    srv_file = tmp_path / "NISMCPServer.py"
    srv_file.write_text("# server")
    assert ij.install(jobs_db, "MCPServer", srv_file, backup=True).startswith("created")
    key, n, task = job(jobs_db, "MCPServer")
    assert key == 2 and n == 1 and task["name"] == "MCPServer"
    script = ij.get_script(task["data"])
    assert script.startswith("# IMPORTANT: 'limjob'") and str(srv_file.resolve()) in script
    compile(script, "loader", "exec")
    assert list(tmp_path.glob("Jobs.before-MCPServer-*.dat"))

    assert ij.install(jobs_db, "MCPServer", srv_file, backup=False).startswith("updated")
    assert job(jobs_db, "MCPServer")[:2] == (2, 2)                      # same job, new program row
    assert ij.get_script(job(jobs_db, "OldPython")[2]["data"]) == "print('old')"   # template untouched


def test_positions_are_rounded(scope):
    scope.x, scope.y, scope.z = 1717.6000000000004, -1399.7000000000003, 488.66000000001
    out, err = tool("status")
    assert not err and out["xy_um"] == [1717.6, -1399.7] and out["z_um"] == 488.66
    out, err = tool("move_relative", dx_um=100, dy_um=0)
    assert not err and out["after_um"] == [1817.6, -1399.7]


def test_focus_down_only_lowers(scope, monkeypatch):
    out, err = tool("focus_down", down_um=50)
    assert not err and out["z_before_um"] == 4619.0 and out["z_after_um"] == 4569.0
    for bad in (-10, 0, 201, float("nan"), "up"):                 # nothing can raise it
        out, err = tool("focus_down", down_um=bad)
        assert err, bad
    monkeypatch.setattr(server, "Z_MIN_UM", 4500.0)
    out, err = tool("focus_down", down_um=100)
    assert err and "below Z_MIN_UM" in out["error"]
    server.ESTOP.write_text("")
    assert "e-stop" in tool("focus_down", down_um=1)[0]["error"]
    assert scope.calls == [("z", -50.0)]


def test_focus_down_reports_a_z_drive_that_did_not_move(scope):
    scope.StgMoveZ = lambda dz, rel: 1                           # accepts, never moves
    out, err = tool("focus_down", down_um=10)
    assert err and "did not go down" in out["error"]


def test_focus_down_rechecks_estop_after_waiting_for_lock(scope, monkeypatch):
    lock = server._nis.lock
    initial_check = threading.Event()
    original = server._stopped

    def observed_check(operation):
        result = original(operation)
        if operation == "focus":
            initial_check.set()
        return result

    monkeypatch.setattr(server, "_stopped", observed_check)
    results = []
    worker = threading.Thread(target=lambda: results.append(tool("focus_down", down_um=10)), daemon=True)
    lock.acquire()
    try:
        worker.start()
        assert initial_check.wait(2), "focus did not reach its initial E-stop check"
        server.estop({"action": "engage", "reason": "mock focus lock-wait stop"})
    finally:
        lock.release()
        worker.join(2)
    assert not worker.is_alive() and len(results) == 1
    out, err = results[0]
    assert err and out["estop_engaged"] and "e-stop" in out["error"]
    assert scope.calls == [] and scope.z == 4619.0
    assert not lock.locked() and server.ESTOP.exists()


def test_focus_down_rechecks_estop_after_position_read(scope, monkeypatch):
    original = server.NIS.position

    def position_then_stop(self):
        result = original(self)
        server.estop({"action": "engage", "reason": "mock focus position-read stop"})
        return result

    monkeypatch.setattr(server.NIS, "position", position_then_stop)
    out, err = tool("focus_down", down_um=10)
    assert err and out["estop_engaged"] and "e-stop" in out["error"]
    assert scope.calls == [] and scope.z == 4619.0
    assert not server._nis.lock.locked() and server.ESTOP.exists()
