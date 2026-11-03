"""pytest test_nis_mcp.py  (the SDK test needs `pip install mcp`; it is skipped without it)"""
import base64
import json
import sqlite3
import threading
import time
import urllib.error
import urllib.request

import pytest

import install_job as ij
import NISMCPServer as server


# --- server ------------------------------------------------------------
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
    assert post(url + "/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"})[0] == 202
    tools = post(url + "/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})[1]["result"]["tools"]
    assert [t["name"] for t in tools] == ["echo", "status", "move_relative", "change_objective",
                                          "list_optical_configs", "capture", "run_experiment",
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
        path.write_text(source.replace(f'"{server.VERSION}"', '"9.9.9"'), encoding="utf-8")
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
        open(path, "wb").close()
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


@pytest.fixture
def scope(monkeypatch, tmp_path):
    fake = FakeScope()
    monkeypatch.setattr(server, "_nis", server.NIS(fake))
    monkeypatch.setattr(server, "ESTOP", tmp_path / "ESTOP")
    monkeypatch.setattr(server, "CAPTURE_DIR", tmp_path / "captures")
    monkeypatch.setattr(server, "_ui", server.MainThread())     # not installed, as off NIS
    return fake


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


def test_status(scope):
    out, err = tool("status")
    assert not err and out == {"xy_um": [100.0, 200.0], "z_um": 4619.0, "objective": "Plan Fluor 4x",
                               "nosepiece_position": 1, "nd_running": False, "estop_engaged": False,
                               "nosepiece": {"0": "Plan Apo 20x", "1": "Plan Fluor 4x", "2": "Plan Apo 10x"}}


def test_move_relative_and_its_refusals(scope):
    out, err = tool("move_relative", dx_um=50, dy_um=-20)
    assert not err and out["before_um"] == [100.0, 200.0] and out["after_um"] == [150.0, 180.0]
    for args in ({"dx_um": 1001, "dy_um": 0}, {"dx_um": float("nan"), "dy_um": 0}, {"dx_um": "far"}):
        assert tool("move_relative", **args)[1]
    server.ESTOP.write_text("")
    out, err = tool("move_relative", dx_um=1, dy_um=0)
    assert err and out["estop_engaged"]
    assert scope.calls == [("move", 50.0, -20.0)]


def test_list_optical_configs(scope):
    assert tool("list_optical_configs") == ({"optical_configs": ["All Devices", "AX", "EPI"]}, False)


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


def test_capture_and_nd_refused_without_the_main_thread_hook(scope):
    for name, args in (("capture", {"filename": "x"}), ("run_experiment", {"experiment": "e"}),
                       ("finish_experiment", {})):
        out, err = tool(name, **args)
        assert err and "main thread" in out["error"], name
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


def test_other_tools_answer_busy_while_an_nd_run_blocks(scope, main_thread, monkeypatch):
    monkeypatch.setattr(server, "BUSY_WAIT_S", 0.2)
    scope.nd_blocks = threading.Event()
    run = threading.Thread(target=tool, args=("run_experiment",), kwargs={"experiment": "long"})
    run.start()
    while "nd_run" not in scope.threads:
        time.sleep(0.01)
    out, err = tool("status")
    assert err and "NISBusy" in out["error"]
    scope.nd_blocks.set()
    run.join()
    assert not tool("status")[1]


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
