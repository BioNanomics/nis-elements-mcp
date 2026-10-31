"""pytest test_nis_mcp.py  (the SDK test needs `pip install mcp`; it is skipped without it)"""
import base64
import json
import sqlite3
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
    assert [t["name"] for t in tools] == ["echo", "change_objective"]
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


# --- change_objective, on a fake scope ---------------------------------
class FakeScope:
    """The macro functions change_objective uses, with a turret that obeys."""

    def __init__(self):
        self.pos, self.cal = 1, "Plan Fluor 4x"
        self.names = {0: "Plan Apo 20x", 1: "Plan Fluor 4x", 2: "Plan Apo 10x"}
        self.calls = []

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
    return fake


def tool(tool_name, **args):
    out = server.call_tool(tool_name, args)
    return json.loads(out["content"][0]["text"]), out["isError"]


def test_change_objective_turns_then_calibrates(scope):
    out, err = tool("change_objective", name="Plan Apo 10x")
    assert not err and out["objective"] == "Plan Apo 10x" and out["before"] == "Plan Fluor 4x"
    assert scope.calls == [("turn", 2), ("calibrate", "Plan Apo 10x")]
    assert tool("change_objective", name="Plan Apo 10x")[0] == {"objective": "Plan Apo 10x", "changed": False}


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
