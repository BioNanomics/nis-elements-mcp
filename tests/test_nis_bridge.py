# The NIS bridge end to end, without NIS: the real bridge_job HTTP server
# and request queue, the real client and the real MCP tools, with a
# stand-in for g5_regprocs.dll that records every macro call. Covers the
# safety rules (POST-only motion, 1 mm cap, 4x/10x only, e-stop in both
# client and bridge, confirm gate, capture paths); what only NIS can
# answer - that the macro calls do the right thing on the Ti2 - is in
# docs/nis-bridge.md's test plan.
import asyncio
import json
import os
import socket
import sys
import threading
import time
import urllib.request

import pytest

from nis_elements_mcp import estop, nis_events, nis_monitor as monitor
from nis_elements_mcp import nis_bridge_client as client_mod
from nis_elements_mcp import nis_bridge_job as bj
from nis_elements_mcp import nis_tools
from nis_elements_mcp.testing import (FakeMainThread, FakeNIS, bridge, estop_file,  # noqa: F401  (fixtures)
                                      events_log, raw)


# ---- bridge behaviour -------------------------------------------------

def test_status_reports_positions_objectives_and_state(bridge):
    nis, url, c = bridge
    s = c.status()
    assert s["xy_um"] == [100.0, 200.0] and s["z_um"] == 4619.0
    assert s["current_objective"] == nis.objectives[1]
    assert s["nosepiece_objectives"] == {str(k): v for k, v in nis.objectives.items()}
    assert s["nd_running"] is False and s["estop_engaged"] is False


def test_relative_move_and_cap(bridge):
    nis, url, c = bridge
    r = c.move_relative(150, -50)
    assert r["before_um"] == [100.0, 200.0] and r["after_um"] == [250.0, 150.0]
    assert "refused" in c.move_relative(1000.1, 0)["error"]
    assert (nis.x, nis.y) == (250.0, 150.0)


def test_get_cannot_move_or_acquire(bridge):
    nis, url, c = bridge
    for path in ("/move", "/objective", "/capture", "/nd/run", "/shutdown"):
        code, out = raw(url, "GET", path)
        assert code == 404
    assert nis.moved() == []


@pytest.mark.parametrize("name,ok", [
    ("Plan Fluor 10x Ph1 DLL", True),
    ("Plan Apo 60x Oil", False),
    ("CFI 14x", False),
    ("Plan Apo 100x Oil", False),
])
def test_objective_allow_list(bridge, name, ok):
    nis, url, c = bridge
    if name not in nis.objectives.values():
        nis.objectives[5] = name
    r = c.change_objective(name)
    assert ("error" not in r) is ok
    assert ("Stg_SetNosepiecePosition" in nis.calls) is ok


def test_objective_change_reports_nosepiece(bridge):
    nis, url, c = bridge
    r = c.change_objective("Plan Fluor 10x Ph1 DLL")
    assert (r["nosepiece_before"], r["nosepiece_after"]) == (1, 2)
    assert r["arrived"] is True and r["after"] == "Plan Fluor 10x Ph1 DLL"
    assert nis.position == 2 and nis.label == "Plan Fluor 10x Ph1 DLL"
    # turret first, then NIS's calibration
    assert nis.calls.index("Stg_SetNosepiecePosition") < nis.calls.index("ChangeObjective")


def test_objective_reports_a_turret_that_does_not_arrive(bridge, monkeypatch):
    nis, url, c = bridge
    monkeypatch.setattr(bj, "TURRET_SETTLE_S", 0.3)
    monkeypatch.setattr(bj, "TURRET_POLL_S", 0.05)
    nis._turn = lambda position: 1          # accepts the command, never turns
    nis.Stg_SetNosepiecePosition = nis._fn("Stg_SetNosepiecePosition", nis._turn)
    r = c.change_objective("Plan Fluor 10x Ph1 DLL")
    assert r["arrived"] is False and r["nosepiece_after"] == 1
    assert "ChangeObjective" not in nis.calls       # no calibration switch for a turret that stayed put


def test_estop_blocks_in_bridge_and_client(bridge, estop_file):
    nis, url, c = bridge
    estop_file.write_text("{}")
    # client refuses before sending anything
    with pytest.raises(estop.EStopEngaged):
        c.move_relative(10, 0)
    # a request that bypasses the client is refused inside the bridge
    for path, body in (("/move", {"dx": 10}), ("/objective", {"name": "Plan Fluor 10x Ph1 DLL"}),
                       ("/capture", {"path": str(estop_file.parent / "a.nd2")}),
                       ("/nd/run", {"experiment": "x"})):
        code, out = raw(url, "POST", path, body)
        assert out.get("estop_engaged") is True, path
    assert nis.moved() == []
    # ending a run is always allowed
    nis.nd_running = True
    assert c.nd_finish()["nd_running"] is False


def test_capture_saves_nd2_and_never_overwrites(bridge, tmp_path):
    nis, url, c = bridge
    target = tmp_path / "check.nd2"
    r = c.capture(str(target))
    assert r["saved"] is True and nis.last_saved == str(target)
    assert nis.threads["Capture"] == nis.threads["ImageSaveAs"] == nis.ui.thread.ident
    assert "already exists" in c.capture(str(target))["error"]
    assert "nd2" in c.capture(str(tmp_path / "x.tif"))["error"]


def test_nd_run_refuses_second_run_and_reports_load_failure(bridge):
    nis, url, c = bridge
    nis.load_rc = -2
    assert "ND_LoadExperiment" in c.nd_run("missing")["error"]
    nis.load_rc = 1
    assert c.nd_run("C. elegans 3h")["nd_running"] is True
    assert "already running" in c.nd_run("C. elegans 3h")["error"]


def test_shutdown_stops_the_bridge(bridge):
    nis, url, c = bridge
    assert c.shutdown() == {"stopping": True}


def test_unreachable_bridge_is_a_clear_error():
    with pytest.raises(client_mod.BridgeUnavailable, match="bridge job"):
        client_mod.NISBridge("http://127.0.0.1:9", timeout_s=2).status()


# ---- MCP tools ----------------------------------------------------------

def test_tools_need_confirm(bridge):
    nis, url, c = bridge
    for call in (lambda: nis_tools.nis_move_relative(10, 0),
                 lambda: nis_tools.nis_change_objective("Plan Fluor 10x Ph1 DLL"),
                 lambda: nis_tools.nis_capture("a"),
                 lambda: nis_tools.nis_run_experiment("x")):
        with pytest.raises(PermissionError):
            call()
    assert nis.moved() == []
    assert nis_tools.nis_move_relative(10, 0, confirm=True)["after_um"] == [110.0, 200.0]


def test_capture_tool_writes_only_into_the_capture_folder(bridge, tmp_path, monkeypatch):
    nis, url, c = bridge
    monkeypatch.setenv("NIS_CAPTURE_DIR", str(tmp_path / "caps"))
    out = nis_tools.nis_capture("aml18_check", confirm=True)
    assert out["path"] == str(tmp_path / "caps" / "aml18_check.nd2")
    for bad in ("../evil", "C:\\Windows\\x", "a/b", ""):
        with pytest.raises(ValueError):
            nis_tools.nis_capture(bad, confirm=True)


def test_nis_server_exposes_exactly_the_nis_tools(tmp_path):
    from mcp.client import Client
    from mcp.client.stdio import StdioServerParameters, stdio_client

    async def names():
        env = dict(os.environ, CONFOCAL_NO_ESTOP_PANEL="1", CONFOCAL_NO_NIS_MONITOR="1",
                   NIS_LOG_DIR=str(tmp_path / "logs"), NIS_CAPTURE_DIR=str(tmp_path / "caps"))
        params = StdioServerParameters(command=sys.executable, args=["-m", "nis_elements_mcp.nis_server"], env=env)
        async with Client(stdio_client(params)) as client:
            return {t.name for t in (await client.list_tools()).tools}

    assert asyncio.run(names()) == {"nis_status", "nis_move_relative", "nis_change_objective",
                                    "nis_capture", "nis_run_experiment", "nis_finish_experiment",
                                    "nis_list_optical_configs", "estop"}


def test_idle_bridge_stops_itself(estop_file):
    nis = FakeNIS()
    t = threading.Thread(target=bj.serve, args=(bj.Bridge(bj.Macro(nis)),),
                         kwargs=dict(port=0, idle_stop_s=0.5), daemon=True)
    t.start()
    t.join(5)
    assert not t.is_alive()


def test_tools_raise_on_bridge_refusals_and_failures(bridge, tmp_path, monkeypatch, estop_file):
    nis, url, c = bridge
    monkeypatch.setenv("NIS_CAPTURE_DIR", str(tmp_path / "caps"))
    with pytest.raises(RuntimeError, match="refused"):                  # move cap
        nis_tools.nis_move_relative(1500, 0, confirm=True)
    with pytest.raises(RuntimeError, match="allowed"):                  # objective not on the allow-list
        nis_tools.nis_change_objective("Plan Apo 60x Oil", confirm=True)
    nis_tools.nis_capture("once", confirm=True)
    with pytest.raises(RuntimeError, match="already exists"):           # never overwrites
        nis_tools.nis_capture("once", confirm=True)
    nis.load_rc = -2
    with pytest.raises(RuntimeError, match="ND_LoadExperiment"):
        nis_tools.nis_run_experiment("missing", confirm=True)


def test_tool_raises_when_the_turret_does_not_arrive(bridge, monkeypatch):
    nis, url, c = bridge
    monkeypatch.setattr(bj, "TURRET_SETTLE_S", 0.3)
    monkeypatch.setattr(bj, "TURRET_POLL_S", 0.05)
    nis.Stg_SetNosepiecePosition = nis._fn("Stg_SetNosepiecePosition", lambda position: 1)
    with pytest.raises(RuntimeError, match='"arrived": false'):
        nis_tools.nis_change_objective("Plan Fluor 10x Ph1 DLL", confirm=True)


# ---- event log and live monitor ---------------------------------------

def test_actions_are_logged_start_then_outcome(bridge, events_log, estop_file):
    nis, url, c = bridge
    c.move_relative(100, 0)                       # ok
    c.move_relative(5000, 0)                      # the bridge refuses the size: failed
    estop_file.write_text("{}")
    with pytest.raises(estop.EStopEngaged):
        c.move_relative(10, 0)                    # refused by the client
    c.nd_finish()                                 # allowed under e-stop
    events, _ = nis_events.read_from(events_log)
    assert [(e["action"], e["phase"]) for e in events] == [
        ("move", "start"), ("move", "ok"), ("move", "start"), ("move", "failed"),
        ("move", "start"), ("move", "refused"), ("nd_finish", "start"), ("nd_finish", "ok")]
    assert events[1]["result"]["after_um"][0] == 200.0 and "elapsed_s" in events[1]
    assert "cap" in nis_events.summarize(events[3]) or "refused" in nis_events.summarize(events[3])


def test_status_reads_are_not_logged_and_unreachable_bridge_is_logged(events_log):
    c = client_mod.NISBridge("http://127.0.0.1:9", timeout_s=1)
    with pytest.raises(client_mod.BridgeUnavailable):
        c.move_relative(1, 0)
    events, _ = nis_events.read_from(events_log)
    assert [(e["action"], e["phase"]) for e in events] == [("move", "start"), ("move", "error")]


def test_log_tail_waits_for_a_complete_line(events_log):
    nis_events.record("move", "start", params={"dx": 1})
    events, offset = nis_events.read_from(events_log)
    assert len(events) == 1
    with events_log.open("a", encoding="utf-8") as f:
        f.write('{"action": "move", "pha')          # a write in progress
    assert nis_events.read_from(events_log, offset) == ([], offset)
    with events_log.open("a", encoding="utf-8") as f:
        f.write('se": "ok"}\n')
    assert [e["phase"] for e in nis_events.read_from(events_log, offset)[0]] == ["ok"]


def test_monitor_names_the_call_the_bridge_is_stuck_on(estop_file):
    import datetime
    now = datetime.datetime(2026, 10, 15, 14, 0, 42)
    start = {"ts": "2026-10-15T14:00:00.000", "action": "nd_run", "phase": "start",
             "params": {"experiment": "bridge test"}}
    assert monitor.pending([start]) == start
    assert monitor.pending([start, {**start, "phase": "ok"}]) is None
    frame = monitor.render("busy", None, [start], "clear", now, colour=False)
    assert "BUSY" in frame and "nd_run" in frame and "42 s so far" in frame
    up = monitor.render("up", {"xy_um": [1.0, 2.0], "z_um": 3.0, "nosepiece_position": 0,
                               "current_objective": "PLAN APO λD 4x OFN25", "nd_running": False},
                        [], "clear", now, colour=False)
    assert "XY 1.0, 2.0" in up and "4x OFN25" in up and "no NIS actions" in up


def test_monitor_status_poll_tells_up_from_down(bridge):
    nis, url, c = bridge
    link, status = monitor.fetch_status(url)
    assert link == "up" and status["xy_um"] == [100.0, 200.0]
    with socket.socket() as s:                    # a port nothing listens on: refused at once
        s.bind(("127.0.0.1", 0))
        closed = s.getsockname()[1]
    assert monitor.fetch_status(f"http://127.0.0.1:{closed}")[0] == "down"


def test_monitor_says_busy_when_the_bridge_connects_but_does_not_answer():
    with socket.socket() as s:                    # accepts connections, never replies
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        old, monitor.STATUS_TIMEOUT_S = monitor.STATUS_TIMEOUT_S, 0.5
        try:
            assert monitor.fetch_status(f"http://127.0.0.1:{s.getsockname()[1]}")[0] == "busy"
        finally:
            monitor.STATUS_TIMEOUT_S = old


def test_monitor_opens_once_when_the_server_starts(monkeypatch):
    opened = []
    monkeypatch.setattr(monitor.subprocess, "Popen", lambda *a, **k: opened.append(a[0]))
    monkeypatch.delenv("CONFOCAL_NO_NIS_MONITOR", raising=False)
    monkeypatch.setattr(monitor, "monitor_running", lambda: False)
    monkeypatch.setattr(monitor, "ON_WINDOWS", True)        # the launch is Windows-only; CI runs on Linux
    monitor.launch()
    assert opened and opened[0][-2:] == ["-m", "nis_elements_mcp.nis_monitor"]
    opened.clear()
    monkeypatch.setattr(monitor, "monitor_running", lambda: True)       # already on screen
    monitor.launch()
    monkeypatch.setattr(monitor, "monitor_running", lambda: False)
    monkeypatch.setenv("CONFOCAL_NO_NIS_MONITOR", "1")                  # switched off
    monitor.launch()
    assert opened == []


def test_optical_configs_can_be_listed_but_not_selected(bridge):
    nis, url, c = bridge
    assert c.optical_configs()["optical_configs"] == ["All Devices", "AX", "EPI"]
    assert nis_tools.nis_list_optical_configs()["optical_configs"] == ["All Devices", "AX", "EPI"]
    code, out = raw(url, "POST", "/optconf", {"name": "AX"})      # selecting froze NIS: no route
    assert code == 404 and nis.moved() == []


def test_a_second_bridge_cannot_share_the_port(bridge):
    nis, url, c = bridge
    port = int(url.rsplit(":", 1)[1])
    with pytest.raises(OSError):
        bj.serve(bj.Bridge(bj.Macro(FakeNIS())), port=port)


def test_ui_calls_run_on_the_main_thread_and_moves_do_not(bridge):
    nis, url, c = bridge
    c.move_relative(10, 0)
    c.nd_run("exp")
    c.nd_finish()
    worker = nis.threads["StgMoveXY"]
    assert worker != nis.ui.thread.ident
    for name in ("ND_LoadExperiment", "ND_RunExperiment", "ND_FinishExperiment"):
        assert nis.threads[name] == nis.ui.thread.ident


def test_ui_calls_are_refused_when_the_main_thread_is_not_hooked(estop_file):
    nis = FakeNIS()
    b = bj.Bridge(bj.Macro(nis))                    # MainThread never installed
    assert "main-thread hook" in b.capture("x.nd2")["error"]
    assert "main-thread hook" in b.nd_run("exp")["error"]
    assert "main-thread hook" in b.nd_finish()["error"]
    assert not nis.calls


def test_a_call_the_main_thread_never_ran_is_cancelled_not_run_late(estop_file):
    ui = bj.MainThread()
    ui.installed = True                             # hooked, but nothing pumps it
    ran = []
    r = ui.call(lambda: ran.append(1) or {}, timeout_s=0.05)
    assert "did not run" in r["error"]
    ui._tick()                                      # the pump finally wakes up
    assert not ran


def test_estop_pressed_while_a_capture_waits_stops_it(estop_file):
    nis = FakeNIS()
    ui = bj.MainThread()
    ui.installed = True
    b = bj.Bridge(bj.Macro(nis), ui)
    box = []
    t = threading.Thread(target=lambda: box.append(b.capture("y.nd2")), daemon=True)
    t.start()
    time.sleep(0.1)                                 # queued, not yet run
    estop_file.write_text("x")
    ui._tick()
    t.join(5)
    assert box[0]["estop_engaged"] is True and "Capture" not in nis.calls
