# The NIS_* settings: defaults, overrides, and the limits the client
# enforces before a request reaches NIS.
import pytest

from nis_elements_mcp import nis_bridge_job as bj
from nis_elements_mcp import nis_tools, settings
from nis_elements_mcp.testing import bridge, estop_file  # noqa: F401  (fixtures)

SETTINGS = ("NIS_BRIDGE_HOST", "NIS_BRIDGE_PORT", "NIS_ESTOP_FILE", "NIS_ALLOWED_OBJECTIVES",
            "NIS_ALLOWED_EXPERIMENTS", "NIS_MAX_STEP_UM", "NIS_NO_ESTOP_PANEL", "NIS_NO_MONITOR")


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    for name in SETTINGS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(settings.Path, "home", lambda: tmp_path)
    return tmp_path


def test_defaults(clean_env):
    assert settings.bridge_url() == "http://127.0.0.1:8766"
    assert settings.estop_file() == clean_env / ".nis-mcp" / "ESTOP"
    assert settings.allowed_magnifications() == (4.0, 10.0)
    assert settings.allowed_experiments() is None
    assert settings.max_step_um() == 1000.0
    assert not settings.estop_panel_disabled() and not settings.monitor_disabled()


def test_overrides(clean_env, monkeypatch):
    monkeypatch.setenv("NIS_BRIDGE_HOST", "10.0.0.5")
    monkeypatch.setenv("NIS_BRIDGE_PORT", "9000")
    monkeypatch.setenv("NIS_ESTOP_FILE", str(clean_env / "stop"))
    monkeypatch.setenv("NIS_ALLOWED_OBJECTIVES", "4x, 10X, 20")
    monkeypatch.setenv("NIS_ALLOWED_EXPERIMENTS", " Embryo 2h , Worm stack ")
    monkeypatch.setenv("NIS_MAX_STEP_UM", "250")
    monkeypatch.setenv("NIS_NO_ESTOP_PANEL", "1")
    monkeypatch.setenv("NIS_NO_MONITOR", "1")
    assert settings.bridge_url() == "http://10.0.0.5:9000"
    assert settings.estop_file() == clean_env / "stop"
    assert settings.allowed_magnifications() == (4.0, 10.0, 20.0)
    assert settings.allowed_experiments() == ["Embryo 2h", "Worm stack"]
    assert settings.max_step_um() == 250.0
    assert settings.estop_panel_disabled() and settings.monitor_disabled()


def test_magnification_is_read_from_the_objective_name():
    assert bj.magnification("PLAN APO λD 10x OFN25 DIC N1") == 10.0
    assert bj.magnification("Plan Apo 60x Oil") == 60.0
    assert bj.magnification("Plan Fluor 14x") == 14.0          # not the 4x inside it
    assert bj.magnification("no magnification") is None
    assert bj.magnification("4x or 10x") is None                # ambiguous: refused


def test_a_step_over_the_limit_is_refused_before_it_is_sent(bridge, monkeypatch):
    nis, url, c = bridge
    monkeypatch.setenv("NIS_MAX_STEP_UM", "200")
    assert "NIS_MAX_STEP_UM" in c.move_relative(250, 0)["error"]
    assert c.move_relative(150, 0)["after_um"] == [250.0, 200.0]
    assert nis.moved() == ["StgMoveXY"]                         # only the allowed step reached NIS


def test_an_objective_outside_the_allow_list_is_refused_before_it_is_sent(bridge, monkeypatch):
    nis, url, c = bridge
    monkeypatch.setenv("NIS_ALLOWED_OBJECTIVES", "10")
    with pytest.raises(RuntimeError, match="NIS_ALLOWED_OBJECTIVES"):
        nis_tools.nis_change_objective(nis.objectives[1], confirm=True)     # the 4x
    assert nis.moved() == []
    assert nis_tools.nis_change_objective("Plan Fluor 10x Ph1 DLL", confirm=True)["arrived"] is True


def test_the_bridge_keeps_its_own_objective_limit(bridge, monkeypatch):
    nis, url, c = bridge
    monkeypatch.setenv("NIS_ALLOWED_OBJECTIVES", "4,10,60")      # the client would allow 60x ...
    assert "only 4x/10x" in c.change_objective("Plan Apo 60x Oil")["error"]   # ... the bridge does not
    assert nis.moved() == []


def test_only_listed_experiments_run_when_the_list_is_set(bridge, monkeypatch):
    nis, url, c = bridge
    monkeypatch.setenv("NIS_ALLOWED_EXPERIMENTS", "Embryo 2h,Worm stack")
    with pytest.raises(RuntimeError, match="NIS_ALLOWED_EXPERIMENTS"):
        nis_tools.nis_run_experiment("Something else", confirm=True)
    assert "ND_LoadExperiment" not in nis.calls
    assert nis_tools.nis_run_experiment("Worm stack", confirm=True)["nd_running"] is True
