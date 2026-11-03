# The STOP panel against a fake tkinter (no display, no tk needed), plus the
# Windows single-instance mutex paths of the panel and the NIS monitor against
# a fake kernel32, so they run on a Linux CI runner too.
import ctypes
import importlib
import sys
import types
from types import SimpleNamespace as NS

import pytest

from nis_elements_mcp import estop, nis_monitor


class Widget:
    def __init__(self, master=None, **kw):
        self.master, self.opts, self.binds, self.packed = master, dict(kw), {}, None
        self.x, self.y = 5, 7

    def pack(self, **kw):
        self.packed = kw

    def configure(self, **kw):
        self.opts.update(kw)

    config = configure

    def bind(self, event, fn):
        self.binds[event] = fn

    def click(self):
        self.opts["command"]()


class FakeTk(Widget):
    def __init__(self):
        super().__init__()
        self.geometry_calls, self.attrs, self.after_calls = [], {}, []
        self.destroyed = self.looped = self.borderless = False

    def title(self, t):
        self.opts["title"] = t

    def resizable(self, a, b):
        pass

    def attributes(self, name, value):
        self.attrs[name] = value

    def overrideredirect(self, flag):
        self.borderless = flag

    def geometry(self, g):
        self.geometry_calls.append(g)

    def after(self, ms, fn):
        self.after_calls.append((ms, fn))

    def winfo_x(self):
        return self.x

    def winfo_y(self):
        return self.y

    def mainloop(self):
        self.looped = True

    def destroy(self):
        self.destroyed = True


class TclError(Exception):
    pass


@pytest.fixture
def panel_mod(monkeypatch, tmp_path):
    asked = {"yes": True, "errors": []}
    tk = types.ModuleType("tkinter")
    tk.Tk, tk.Button, tk.Label, tk.Frame, tk.TclError = FakeTk, Widget, Widget, Widget, TclError
    mb = types.ModuleType("tkinter.messagebox")
    mb.askyesno = lambda title, msg: asked["yes"]
    mb.showerror = lambda title, msg: asked["errors"].append(msg)
    tk.messagebox = mb
    monkeypatch.setitem(sys.modules, "tkinter", tk)
    monkeypatch.setitem(sys.modules, "tkinter.messagebox", mb)
    monkeypatch.setattr(estop, "ESTOP_PATH", tmp_path / "ESTOP")
    sys.modules.pop("nis_elements_mcp.estop_panel", None)
    mod = importlib.import_module("nis_elements_mcp.estop_panel")
    mod.real_claim = mod._claim_single_instance                    # the tests of the mutex use this
    monkeypatch.setattr(mod, "_claim_single_instance", lambda: True)   # a real mutex would outlive the test
    yield mod, asked
    sys.modules.pop("nis_elements_mcp.estop_panel", None)


def make_panel(mod, **kw):
    root = FakeTk()
    return mod.Panel(root, **kw), root


def test_panel_starts_clear_and_polls(panel_mod):
    mod, _ = panel_mod
    p, root = make_panel(mod)
    assert p.status.opts["text"] == "clear - motion permitted"
    assert p.release_btn.opts["state"] == "disabled" and p.button.opts["text"] == "STOP"
    assert root.attrs["-topmost"] is True and root.borderless is True
    assert root.after_calls[0][0] == mod.POLL_MS


def test_panel_window_options(panel_mod):
    mod, _ = panel_mod
    p, root = make_panel(mod, topmost=False, borderless=False)
    assert "-topmost" not in root.attrs and root.borderless is False


def test_stop_button_engages_and_the_panel_shows_it(panel_mod):
    mod, _ = panel_mod
    p, _ = make_panel(mod)
    p.button.click()
    assert estop.is_engaged() and estop.details()["reason"] == "STOP button"
    assert p.status.opts["text"].startswith("ENGAGED") and p.button.opts["text"] == "STOPPED"
    assert p.release_btn.opts["state"] == "normal"


def test_stop_failure_is_reported_but_never_raises(panel_mod, monkeypatch):
    mod, asked = panel_mod
    p, _ = make_panel(mod)
    monkeypatch.setattr(estop, "engage", lambda reason: (_ for _ in ()).throw(OSError("disk full")))
    p.on_stop()
    assert "disk full" in asked["errors"][0]


def test_release_needs_confirmation(panel_mod):
    mod, asked = panel_mod
    p, _ = make_panel(mod)
    p.on_release()                                  # nothing engaged: nothing to ask
    estop.engage("t")
    asked["yes"] = False
    p.on_release()
    assert estop.is_engaged()
    asked["yes"] = True
    p.release_btn.click()
    assert not estop.is_engaged() and p.button.opts["text"] == "STOP"


def test_refresh_only_redraws_on_change(panel_mod):
    mod, _ = panel_mod
    p, _ = make_panel(mod)
    p.status.opts["text"] = "sentinel"
    p.refresh()
    assert p.status.opts["text"] == "sentinel"       # unchanged state: left alone
    p.refresh(force=True)
    assert p.status.opts["text"] == "clear - motion permitted"


def test_tick_reschedules_itself_and_sees_an_outside_engage(panel_mod):
    mod, _ = panel_mod
    p, root = make_panel(mod)
    estop.engage("from a terminal")
    root.after_calls[-1][1]()
    assert p.status.opts["text"].startswith("ENGAGED") and len(root.after_calls) == 2


def test_dragging_moves_the_window(panel_mod):
    mod, _ = panel_mod
    p, root = make_panel(mod)
    p.status.binds["<Button-1>"](NS(x_root=100, y_root=100))
    p.status.binds["<B1-Motion>"](NS(x_root=130, y_root=150))
    assert root.geometry_calls[-1] == "+35+57"


def test_single_instance_claim_off_windows_is_always_true(panel_mod, monkeypatch):
    mod, _ = panel_mod
    monkeypatch.setattr(sys, "platform", "linux")
    assert mod.real_claim() is True


def fake_kernel(monkeypatch, *, exists, last_error=0):
    calls = []
    k32 = NS(OpenMutexW=lambda *a: 99 if exists else 0,
             CloseHandle=lambda h: calls.append(("close", h)),
             CreateMutexW=NS(restype=None) and (lambda *a: 7),
             GetLastError=lambda: last_error)
    # CreateMutexW needs a settable .restype, so use a callable object
    class Create:
        restype = None

        def __call__(self, *a):
            calls.append(("create", a[-1] if a else None))
            return 7

    k32.CreateMutexW = Create()
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "windll", NS(kernel32=k32), raising=False)
    return calls


def test_panel_claims_the_windows_mutex_once(panel_mod, monkeypatch):
    mod, _ = panel_mod
    fake_kernel(monkeypatch, exists=False, last_error=0)
    assert mod.real_claim() is True
    fake_kernel(monkeypatch, exists=True, last_error=183)
    assert mod.real_claim() is False


def test_main_opens_the_panel(panel_mod, monkeypatch):
    mod, _ = panel_mod
    made = []
    monkeypatch.setattr(mod.tk, "Tk", lambda: made.append(FakeTk()) or made[-1])
    monkeypatch.setattr(sys, "argv", ["estop_panel", "--no-topmost", "--titlebar"])
    mod.main()
    assert made[0].looped and "-topmost" not in made[0].attrs and made[0].borderless is False


def test_main_does_nothing_if_a_panel_is_already_open(panel_mod, monkeypatch, capsys):
    mod, _ = panel_mod
    monkeypatch.setattr(mod, "_claim_single_instance", lambda: False)
    monkeypatch.setattr(sys, "argv", ["estop_panel"])
    mod.main()
    assert "already open" in capsys.readouterr().err


def test_main_explains_a_missing_display(panel_mod, monkeypatch, capsys):
    mod, _ = panel_mod

    def no_display():
        raise TclError("no display name")

    monkeypatch.setattr(mod.tk, "Tk", no_display)
    monkeypatch.setattr(sys, "argv", ["estop_panel"])
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 1 and "no display available" in capsys.readouterr().err


# ---- Windows mutex probes in estop and nis_monitor ----------------------------------

def test_estop_panel_running_reads_the_windows_mutex(monkeypatch):
    calls = fake_kernel(monkeypatch, exists=True)
    assert estop.panel_running() is True and ("close", 99) in calls
    fake_kernel(monkeypatch, exists=False)
    assert estop.panel_running() is False


def test_monitor_running_reads_the_windows_mutex(monkeypatch):
    calls = fake_kernel(monkeypatch, exists=True)
    assert nis_monitor.monitor_running() is True and ("close", 99) in calls
    fake_kernel(monkeypatch, exists=False)
    assert nis_monitor.monitor_running() is False


def test_monitor_mutex_is_single_instance(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert nis_monitor._hold_mutex() is True
    fake_kernel(monkeypatch, exists=False, last_error=0)
    assert nis_monitor._hold_mutex() == 7
    fake_kernel(monkeypatch, exists=True, last_error=183)
    assert nis_monitor._hold_mutex() is None


def test_monitor_main_refuses_a_second_window(monkeypatch):
    monkeypatch.setattr(nis_monitor, "_hold_mutex", lambda: None)
    monkeypatch.setattr(sys, "argv", ["nis_monitor"])
    with pytest.raises(SystemExit, match="already open"):
        nis_monitor.main()


def test_monitor_main_loop_runs_until_interrupted(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(nis_monitor, "_hold_mutex", lambda: 1)
    monkeypatch.setattr(nis_monitor.nis_events, "events_dir", lambda: tmp_path / "ev")
    monkeypatch.setattr(nis_monitor, "fetch_status", lambda url: ("down", None))
    monkeypatch.setattr(estop, "ESTOP_PATH", tmp_path / "ESTOP")
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        if len(sleeps) >= 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(nis_monitor.time, "sleep", sleep)
    monkeypatch.setattr(sys, "argv", ["nis_monitor", "--interval", "0.5"])
    nis_monitor.main()
    assert sleeps == [0.5, 0.5] and "NIS monitor" in capsys.readouterr().out


def test_monitor_main_starts_a_new_log_after_midnight(monkeypatch, tmp_path, capsys):
    import datetime
    days = iter([datetime.date(2026, 10, 15), datetime.date(2026, 10, 15), datetime.date(2026, 10, 16),
                 datetime.date(2026, 10, 16), datetime.date(2026, 10, 16)])
    monkeypatch.setattr(nis_monitor, "_hold_mutex", lambda: 1)
    monkeypatch.setattr(nis_monitor.nis_events, "events_dir", lambda: tmp_path / "ev")
    monkeypatch.setattr(nis_monitor.nis_events, "events_path",
                        lambda day=None: tmp_path / f"{next(days).isoformat()}.jsonl")
    monkeypatch.setattr(nis_monitor, "fetch_status", lambda url: ("down", None))
    monkeypatch.setattr(estop, "ESTOP_PATH", tmp_path / "ESTOP")
    n = []
    monkeypatch.setattr(nis_monitor.time, "sleep", lambda s: (n.append(1), n.__len__() > 1 and (_ for _ in ()).throw(KeyboardInterrupt))[0])
    monkeypatch.setattr(sys, "argv", ["nis_monitor"])
    nis_monitor.main()
