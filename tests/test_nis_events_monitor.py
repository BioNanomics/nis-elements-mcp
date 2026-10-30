# Tests for the NIS worker-thread entry point, optical configs, event log and
# live monitor. Reuses the fake NIS and the bridge fixture from test_nis_bridge.
# No NIS, no Windows and no hardware needed, so they run on the CI runner.

import datetime
import sys
import threading

import pytest

from nis_elements_mcp import nis_events, nis_monitor as monitor
from nis_elements_mcp import nis_bridge_client as client_mod
from nis_elements_mcp import nis_bridge_job as bj
from nis_elements_mcp import nis_tools
from nis_elements_mcp.testing import bridge, estop_file, events_log, raw  # noqa: F401  (fixtures)

NOW = datetime.datetime(2026, 10, 15, 14, 0, 0)


# ---- JOBS entry point ---------------------------------------------------

def test_run_returns_at_once_and_serves_from_a_worker_thread(monkeypatch):
    # A JOBS Python task runs on NIS's main thread: run() must not block it.
    started, release, seen = threading.Event(), threading.Event(), {}

    def fake_serve(bridge_obj):
        seen["thread"], seen["bridge"] = threading.current_thread(), bridge_obj
        started.set()
        release.wait(5)

    monkeypatch.setattr(bj, "Macro", lambda: "macro")
    hooked = []
    monkeypatch.setattr(bj, "MainThread", lambda: type("UI", (), {"install": lambda self: hooked.append(
        threading.current_thread())})())
    monkeypatch.setattr(bj, "Bridge", lambda m, ui: ("bridge", m))
    monkeypatch.setattr(bj, "serve", fake_serve)
    bj.run((), None, None, None)               # returns although fake_serve is still going
    assert started.wait(5)
    assert seen["thread"] is not threading.main_thread()
    assert seen["thread"].daemon and seen["thread"].name == "nis-bridge"
    assert seen["bridge"] == ("bridge", "macro")
    assert hooked == [threading.current_thread()]      # the main-thread hook is installed from run()'s thread
    release.set()


# ---- optical configurations ----------------------------------------------

def test_optical_configs_skip_blanks_and_duplicates(bridge):
    nis, url, c = bridge
    nis.opt_confs = ["AX", "", "AX", "EPI"]
    assert c.optical_configs()["optical_configs"] == ["AX", "EPI"]
    assert nis.moved() == []


def test_optical_configs_are_get_only_and_not_logged(bridge, events_log):
    nis, url, c = bridge
    code, _ = raw(url, "POST", "/optconfs", {})
    assert code == 404
    c.optical_configs()
    assert nis_events.read_from(events_log) == ([], 0)


def test_cli_lists_optical_configs(bridge, capsys, monkeypatch):
    nis, url, c = bridge
    monkeypatch.setenv("CONFOCAL_NIS_BRIDGE_URL", url)
    monkeypatch.setattr(sys, "argv", ["nis_bridge", "optconfs"])
    client_mod.main()
    assert "All Devices" in capsys.readouterr().out


# ---- MCP tool helpers ------------------------------------------------------

def test_checked_passes_good_answers_and_raises_on_failures():
    assert nis_tools._checked({"saved": True, "path": "a.nd2"}) == {"saved": True, "path": "a.nd2"}
    for bad in ({"error": "refused"}, {"arrived": False}, {"saved": False}):
        with pytest.raises(RuntimeError, match="NIS bridge"):
            nis_tools._checked(bad)


def test_capture_folder_defaults_under_home_nis_mcp(tmp_path, monkeypatch):
    monkeypatch.delenv("NIS_CAPTURE_DIR", raising=False)
    monkeypatch.setattr(nis_tools.Path, "home", lambda: tmp_path)
    d = nis_tools.capture_dir()
    assert d == tmp_path / "nis-mcp" / "captures" and d.is_dir()


def test_capture_folder_follows_nis_capture_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("NIS_CAPTURE_DIR", str(tmp_path / "mine"))
    assert nis_tools.capture_dir() == tmp_path / "mine"


def test_event_log_folder_follows_nis_log_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("NIS_LOG_DIR", str(tmp_path / "logs"))
    assert nis_events.events_dir() == tmp_path / "logs"
    monkeypatch.delenv("NIS_LOG_DIR")
    monkeypatch.setattr(nis_events.Path, "home", lambda: tmp_path)
    assert nis_events.events_dir() == tmp_path / "nis-mcp" / "logs"


# ---- event log -------------------------------------------------------------

def test_capture_and_nd_run_are_logged(bridge, events_log, tmp_path):
    nis, url, c = bridge
    c.capture(str(tmp_path / "log.nd2"))
    c.nd_run("exp")
    events, _ = nis_events.read_from(events_log)
    assert [(e["action"], e["phase"]) for e in events] == [
        ("capture", "start"), ("capture", "ok"), ("nd_run", "start"), ("nd_run", "ok")]


def test_recording_never_raises_when_the_log_cannot_be_written(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setattr(nis_events, "events_dir", lambda: blocker / "sub")   # a folder cannot be made here
    e = nis_events.record("move", "start", params={"dx": 1})
    assert e["action"] == "move" and e["phase"] == "start"


def test_reading_a_missing_or_damaged_log(tmp_path):
    assert nis_events.read_from(tmp_path / "none.jsonl", 7) == ([], 7)
    p = tmp_path / "events.jsonl"
    p.write_bytes(b'not json\n{"action": "move", "phase": "ok"}\n')
    events, offset = nis_events.read_from(p)
    assert events == [{"action": "move", "phase": "ok"}] and offset == p.stat().st_size


def test_events_go_to_one_file_per_day(events_log):
    assert events_log.name == f"events-{datetime.date.today().isoformat()}.jsonl"
    assert nis_events.events_path(datetime.date(2026, 10, 15)).name == "events-2026-10-15.jsonl"


def test_summarize_each_kind_of_event():
    s = nis_events.summarize
    assert s({"action": "move", "phase": "start", "params": {"dx": 1, "dy": 2}}) == "dx=1 dy=2"
    assert s({"action": "move", "phase": "refused", "message": "e-stop"}) == "e-stop"
    assert s({"action": "move", "phase": "failed", "result": {"error": "too far"}}) == "too far"
    assert s({"action": "move", "phase": "ok", "elapsed_s": 1.5,
              "result": {"before_um": [1, 2], "after_um": [3.5, 4]}}) == "(1.0, 2.0) -> (3.5, 4.0)  (1.5 s)"
    assert s({"action": "move", "phase": "ok", "result": {}}) == "? -> ?"
    assert "arrived=True" in s({"action": "objective", "phase": "ok", "result": {
        "before": "a", "after": "b", "arrived": True, "nosepiece_before": 1, "nosepiece_after": 2}})
    assert s({"action": "capture", "phase": "ok",
              "result": {"saved": True, "path": "p.nd2"}}) == "saved=True p.nd2"
    assert s({"action": "nd_run", "phase": "ok", "result": {"nd_running": True}}) == "nd_running=True"
    assert s({"action": "shutdown", "phase": "ok", "result": {}}) == ""


# ---- monitor ---------------------------------------------------------------

def test_pending_is_the_latest_unclosed_start():
    a = {"ts": "2026-10-15T14:00:00.000", "action": "capture", "phase": "start"}
    b = {"ts": "2026-10-15T14:00:05.000", "action": "nd_run", "phase": "start"}
    assert monitor.pending([a, b]) == b
    assert monitor.pending([a, b, {**b, "phase": "failed"}]) == a
    assert monitor.pending([]) is None


def test_render_shows_link_state_estop_and_recent_events(estop_file):
    ev = [{"ts": f"2026-10-15T13:00:0{i}.000", "action": "move", "phase": "ok",
           "result": {"before_um": [0, 0], "after_um": [i, 0]}} for i in range(5)]
    down = monitor.render("down", None, ev, "clear", NOW, rows=2, colour=False)
    assert "DOWN" in down and "e-stop: clear" in down
    assert "13:00:04" in down and "13:00:03" in down and "13:00:02" not in down   # only the last `rows`
    assert "\x1b[" not in down
    estop_file.write_text("{}")
    assert "e-stop: ENGAGED" in monitor.render("up", None, [], "ENGAGED", NOW, colour=False)
    assert "\x1b[" in monitor.render("up", None, [], "ENGAGED", NOW, colour=True)


def test_render_truncates_long_lines(estop_file):
    ev = [{"ts": "2026-10-15T13:00:00.000", "action": "capture", "phase": "ok",
           "result": {"saved": True, "path": "x" * 300}}]
    frame = monitor.render("up", None, ev, "clear", NOW, width=60, colour=False)
    assert all(len(line) <= 60 for line in frame.splitlines()) and "…" in frame


def test_fetch_status_treats_a_garbled_answer_as_down(monkeypatch):
    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"<html>"

    monkeypatch.setattr(monitor.urllib.request, "urlopen", lambda *a, **k: Resp())
    assert monitor.fetch_status("http://x") == ("down", None)


def test_monitor_launch_does_nothing_off_windows(monkeypatch):
    opened = []
    monkeypatch.setattr(monitor.subprocess, "Popen", lambda *a, **k: opened.append(a))
    monkeypatch.delenv("CONFOCAL_NO_NIS_MONITOR", raising=False)
    monkeypatch.setattr(monitor, "ON_WINDOWS", False)
    monitor.launch()
    assert opened == []


def test_monitor_launch_failure_is_reported_not_raised(monkeypatch, capsys):
    def boom(*a, **k):
        raise OSError("no console")

    monkeypatch.setattr(monitor.subprocess, "Popen", boom)
    monkeypatch.delenv("CONFOCAL_NO_NIS_MONITOR", raising=False)
    monkeypatch.setattr(monitor, "ON_WINDOWS", True)
    monkeypatch.setattr(monitor, "monitor_running", lambda: False)
    monitor.launch()
    assert "could not open the monitor window" in capsys.readouterr().err


def test_monitor_once_prints_one_snapshot(bridge, events_log, capsys, monkeypatch):
    nis, url, c = bridge
    c.move_relative(10, 0)
    monkeypatch.setattr(sys, "argv", ["nis_monitor", "--once", "--url", url])
    monitor.main()
    out = capsys.readouterr().out
    assert "bridge: UP" in out and "XY 110.0, 200.0" in out and "move" in out
