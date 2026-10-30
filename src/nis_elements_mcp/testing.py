# testing.py
# ------------------------------------------------------------
# A mock NIS for tests, no microscope needed: FakeNIS stands in for
# g5_regprocs.dll (same function names, records every call), and the
# `bridge` fixture runs the real bridge job's HTTP server and request queue
# on it, on a free port, with the client pointed at it.
#
# Use it from any project's tests (needs pytest):
#
#   from nis_elements_mcp.testing import bridge, estop_file  # noqa: F401  (fixtures)
#
#   def test_my_script_moves(bridge):
#       nis, url, client = bridge
#       client.move_relative(100, 0)
#       assert nis.x == 200.0
# ------------------------------------------------------------

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from nis_elements_mcp import estop, nis_events
from nis_elements_mcp import nis_bridge_client as client_mod
from nis_elements_mcp import nis_bridge_job as bj


class FakeNIS:
    """Stands in for g5_regprocs.dll: same names, records calls."""

    def __init__(self):
        self.x, self.y, self.z = 100.0, 200.0, 4619.0
        self.objectives = {1: "PLAN APO λD 4x OFN25", 2: "Plan Fluor 10x Ph1 DLL",
                           4: "Plan Apo 60x Oil"}
        self.position = 1
        self.label = self.objectives[1]     # NIS's objective name: only ChangeObjective moves it
        self.nd_running = False
        self.load_rc = 1
        self.calls = []
        self.threads = {}               # call name -> id of the thread that made it
        self.last_saved = None
        f = self._fn
        self.StgGetPosXY = f("StgGetPosXY", lambda px, py: self._set(px, self.x, py, self.y))
        self.StgGetPosZ = f("StgGetPosZ", lambda pz, dev: self._set(pz, self.z))
        self.StgMoveXY = f("StgMoveXY", self._move)
        self.Stg_GetNosepiecePosition = f("Stg_GetNosepiecePosition", lambda: self.position)
        self.Stg_GetNosepiecePositions = f("Stg_GetNosepiecePositions", lambda: 6)
        self.Stg_GetNosepieceObjectiveName = f("Stg_GetNosepieceObjectiveName", self._name)
        self.GetCurrentObjName = f("GetCurrentObjName", self._current)
        self.Stg_SetNosepiecePosition = f("Stg_SetNosepiecePosition", self._turn)
        self.ChangeObjective = f("ChangeObjective", self._change)
        self.opt_confs = ["All Devices", "AX", "EPI"]
        self.GetOptConfCount = f("GetOptConfCount", lambda: len(self.opt_confs))
        self.GetOptConfName = f("GetOptConfName", self._conf_name)
        self.Capture = f("Capture", lambda: 1)
        self.ImageSaveAs = f("ImageSaveAs", self._save)
        self.CloseCurrentDocument = f("CloseCurrentDocument", lambda save: 1)
        self.ND_LoadExperiment = f("ND_LoadExperiment", lambda name: self.load_rc)
        self.ND_RunExperiment = f("ND_RunExperiment", self._run)
        self.ND_IsInExperimentCapture = f("ND_IsInExperimentCapture", lambda: int(self.nd_running))
        self.ND_FinishExperiment = f("ND_FinishExperiment", self._finish)

    def _fn(self, name, impl):
        def call(*args):
            self.calls.append(name)
            self.threads[name] = threading.get_ident()
            return impl(*args)
        return call

    @staticmethod
    def _set(*pairs):
        for ptr, value in zip(pairs[::2], pairs[1::2]):
            ptr._obj.value = value
        return 1

    def _move(self, dx, dy, relative):
        assert relative == bj.MOVE_RELATIVE
        self.x += dx; self.y += dy
        return 1

    def _name(self, i, buf, n):
        if i in self.objectives:
            buf.value = self.objectives[i]
            return 1
        return -2

    def _current(self, buf):
        buf.value = self.label
        return 1

    def _change(self, name):
        self.label = name                   # calibration only; the turret stays put
        return 1

    def _turn(self, position):
        if position not in self.objectives:
            return -2
        self.position = position
        return 1

    def _save(self, path, kind, compression):
        assert kind == bj.ND2_ALL_LAYERS
        open(path, "wb").close()
        self.last_saved = path
        return 1

    def _run(self, open_after):
        self.nd_running = True
        return 1

    def _finish(self):
        self.nd_running = False
        return 1

    def _conf_name(self, i, buf, n):
        buf.value = self.opt_confs[i] if 0 <= i < len(self.opt_confs) else ""
        return 1

    def moved(self):
        return [c for c in self.calls if c in ("StgMoveXY", "Stg_SetNosepiecePosition", "ChangeObjective", "Capture",
                                               "ND_RunExperiment")]


@pytest.fixture
def estop_file(tmp_path, monkeypatch):
    path = tmp_path / "ESTOP"
    monkeypatch.setattr(estop, "ESTOP_PATH", path)
    monkeypatch.setattr(bj, "ESTOP", path)
    return path


class FakeMainThread(bj.MainThread):
    """Plays NIS's message loop: one thread that calls _tick, as the Win32 timer does."""

    def __init__(self):
        super().__init__(tick_ms=5)
        self.installed = True
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._pump, daemon=True)
        self.thread.start()
        self.main_thread_id = self.thread.ident

    def _pump(self):
        while not self._stop.is_set():
            self._tick()
            time.sleep(0.005)

    def close(self):
        self._stop.set()
        self.thread.join(5)


@pytest.fixture
def bridge(estop_file, monkeypatch):
    nis = FakeNIS()
    ui = FakeMainThread()
    stop, ready, port = threading.Event(), threading.Event(), []
    t = threading.Thread(target=bj.serve, args=(bj.Bridge(bj.Macro(nis), ui),),
                         kwargs=dict(port=0, stop=stop,
                                     on_ready=lambda p: (port.append(p), ready.set())),
                         daemon=True)
    t.start()
    assert ready.wait(5)
    url = f"http://127.0.0.1:{port[0]}"
    monkeypatch.setenv("CONFOCAL_NIS_BRIDGE_URL", url)
    nis.ui = ui
    yield nis, url, client_mod.NISBridge(url)
    stop.set()
    t.join(5)
    ui.close()


@pytest.fixture
def events_log(tmp_path, monkeypatch):
    monkeypatch.setattr(nis_events, "events_dir", lambda: tmp_path / "events")
    return nis_events.events_path()


def raw(url, method, path, body=None):
    req = urllib.request.Request(url + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())
