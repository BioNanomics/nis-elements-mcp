import json
import sys

import pytest

from nis_elements_mcp import estop


@pytest.fixture
def flag(tmp_path, monkeypatch):
    path = tmp_path / "sub" / "ESTOP"
    monkeypatch.setattr(estop, "ESTOP_PATH", path)
    return path


def test_clear_by_default(flag):
    assert estop.is_engaged() is False
    assert estop.details() is None
    estop.check()                                   # no raise


def test_engage_writes_flag_and_blocks_motion(flag):
    info = estop.engage("test reason")
    assert flag.exists() and info["reason"] == "test reason"
    assert estop.is_engaged()
    assert estop.details()["reason"] == "test reason"
    with pytest.raises(estop.EStopEngaged, match="test reason"):
        estop.check()


def test_release_allows_motion_and_is_idempotent(flag):
    estop.engage()
    estop.release()
    assert not estop.is_engaged()
    estop.release()                                 # already clear: no error
    estop.check()


def test_unreadable_flag_still_counts_as_engaged(flag):
    flag.parent.mkdir(parents=True)
    flag.write_text("not json")
    assert estop.is_engaged()
    assert "unreadable" in estop.details()["reason"]
    with pytest.raises(estop.EStopEngaged):
        estop.check()


def test_fails_safe_when_state_cannot_be_determined(monkeypatch):
    class Broken:
        def exists(self):
            raise PermissionError("denied")

    monkeypatch.setattr(estop, "ESTOP_PATH", Broken())
    assert estop.is_engaged() is True


def test_panel_not_running_off_windows(monkeypatch):
    monkeypatch.setattr(estop.sys, "platform", "linux")
    assert estop.panel_running() is False


def test_launch_panel_opt_out_and_already_running(monkeypatch):
    opened = []
    monkeypatch.setattr(estop.subprocess, "Popen", lambda *a, **k: opened.append(a))
    monkeypatch.setenv("NIS_NO_ESTOP_PANEL", "1")
    estop.launch_panel()
    monkeypatch.delenv("NIS_NO_ESTOP_PANEL")
    monkeypatch.setattr(estop, "panel_running", lambda: True)
    estop.launch_panel()
    assert opened == []


def test_launch_panel_starts_detached_process(monkeypatch):
    opened = []
    monkeypatch.delenv("NIS_NO_ESTOP_PANEL", raising=False)
    monkeypatch.setattr(estop, "panel_running", lambda: False)
    monkeypatch.setattr(estop.subprocess, "Popen", lambda *a, **k: opened.append(a[0]))
    estop.launch_panel()
    assert opened[0][-2:] == ["-m", "nis_elements_mcp.estop_panel"]


def test_launch_panel_failure_goes_to_stderr(monkeypatch, capsys):
    def boom(*a, **k):
        raise OSError("nope")

    monkeypatch.delenv("NIS_NO_ESTOP_PANEL", raising=False)
    monkeypatch.setattr(estop, "panel_running", lambda: False)
    monkeypatch.setattr(estop.subprocess, "Popen", boom)
    estop.launch_panel()
    out = capsys.readouterr()
    assert "could not start the STOP panel" in out.err and out.out == ""


def run_cli(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["estop", *argv])
    estop.main()


def test_cli_engage_status_release(flag, monkeypatch, capsys):
    run_cli(monkeypatch, "status")
    assert "clear" in capsys.readouterr().out
    run_cli(monkeypatch, "engage", "--reason", "cli")
    assert "E-STOP ENGAGED" in capsys.readouterr().out
    assert json.loads(flag.read_text())["reason"] == "cli"
    with pytest.raises(SystemExit) as exc:
        run_cli(monkeypatch, "status")
    assert exc.value.code == 2 and "ENGAGED" in capsys.readouterr().out
    run_cli(monkeypatch, "release")
    assert "released" in capsys.readouterr().out and not flag.exists()
