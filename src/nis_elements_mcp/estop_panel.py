# estop_panel.py
# ------------------------------------------------------------
# An always-on-top STOP button for the microscope.
#
#   python -m nis_elements_mcp.estop_panel
#
# Small, borderless, stays above other windows, drag it anywhere. One big
# button. Hitting it engages nis_elements_mcp.estop, which every motion
# primitive checks before it moves - see that module's header.
#
# WHY A WINDOW AND NOT JUST THE CLI: during the 2026-09-21 incident the
# stage was moving while the operator hunted for a terminal, typed a
# command, and waited for python to start. Seconds matter and typing does
# not scale under stress. A button that is already on screen costs one
# click, with no target to find and nothing to remember.
#
# WHY IT POLLS THE FLAG FILE rather than tracking its own state: the stop
# can also be engaged from a terminal, from the MCP tool, by another
# process, or by a human creating the file. The panel must show what is
# actually true, not what it last did - a panel reading "CLEAR" while the
# stop is engaged elsewhere would be worse than no panel.
#
# WHY RELEASE IS DELIBERATELY AWKWARD: the release control is small,
# visually quiet, and asks for confirmation, while STOP is huge and
# instant. The asymmetry is the point - the cost of an accidental stop is
# a few seconds, the cost of an accidental release is an unguarded
# microscope. Engaging is also idempotent, so panic-clicking is harmless.
#
# ONE PANEL. It opens by itself whenever a process connects to the stage
# (estop.launch_panel), so many processes ask for it. The first takes the
# PANEL_MUTEX; any later copy sees it and exits without drawing.
#
# TKINTER because it ships with CPython: a safety control that depends on
# `pip install` is a safety control that is missing on the day it matters.
# ------------------------------------------------------------

from __future__ import annotations

import argparse
import sys
import tkinter as tk
from tkinter import messagebox

from nis_elements_mcp import estop

POLL_MS = 300

_RED, _RED_DARK = "#c62828", "#8e0000"
_GREEN, _GREY = "#2e7d32", "#9e9e9e"
_BG = "#1b1b1b"


class Panel:
    def __init__(self, root: tk.Tk, topmost: bool = True, borderless: bool = True):
        self.root = root
        root.title("E-STOP")
        root.configure(bg=_BG)
        root.resizable(False, False)
        if topmost:
            root.attributes("-topmost", True)
        if borderless:
            # No title bar - it would be most of a window this small, and
            # the drag handler below replaces the only thing it was for.
            root.overrideredirect(True)
        root.geometry("+40+40")

        self.button = tk.Button(
            root, text="STOP", font=("Segoe UI", 26, "bold"),
            bg=_RED, fg="white", activebackground=_RED_DARK, activeforeground="white",
            relief="raised", bd=3, width=8, height=1, cursor="hand2",
            command=self.on_stop)
        self.button.pack(padx=8, pady=(8, 4))

        self.status = tk.Label(root, text="checking...", font=("Segoe UI", 9, "bold"),
                               bg=_BG, fg=_GREY)
        self.status.pack()

        bar = tk.Frame(root, bg=_BG)
        bar.pack(fill="x", padx=8, pady=(2, 6))
        # Quiet, small, and confirmed - see the header on asymmetry.
        self.release_btn = tk.Button(bar, text="release", font=("Segoe UI", 8),
                                     bg="#2a2a2a", fg="#bbb", relief="flat",
                                     cursor="hand2", command=self.on_release)
        self.release_btn.pack(side="left")
        tk.Button(bar, text="✕", font=("Segoe UI", 8), bg="#2a2a2a", fg="#bbb",
                  relief="flat", cursor="hand2", command=root.destroy).pack(side="right")

        # Drag from anywhere on the background, since there is no title bar.
        for w in (root, self.status, bar):
            w.bind("<Button-1>", self._press)
            w.bind("<B1-Motion>", self._drag)
        self._dx = self._dy = 0

        self._last = None
        self.tick()

    def _press(self, e) -> None:
        self._dx, self._dy = e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y()

    def _drag(self, e) -> None:
        self.root.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def on_stop(self) -> None:
        # Engage first and report afterwards: the flag write is what makes
        # the microscope safe, and it must not wait behind a dialog.
        try:
            estop.engage("STOP button")
        except Exception as exc:
            messagebox.showerror("E-STOP", f"Could not engage:\n{exc}")
        self.refresh(force=True)

    def on_release(self) -> None:
        if not estop.is_engaged():
            return
        if messagebox.askyesno("Release e-stop",
                               "Allow the microscope to move again?\n\n"
                               "Only do this once you know why it stopped."):
            estop.release()
            self.refresh(force=True)

    def refresh(self, force: bool = False) -> None:
        engaged = estop.is_engaged()
        if engaged == self._last and not force:
            return
        self._last = engaged
        if engaged:
            self.status.configure(text="ENGAGED - motion refused", fg=_RED)
            self.button.configure(text="STOPPED", bg=_RED_DARK)
            self.release_btn.configure(state="normal")
        else:
            self.status.configure(text="clear - motion permitted", fg=_GREEN)
            self.button.configure(text="STOP", bg=_RED)
            self.release_btn.configure(state="disabled")

    def tick(self) -> None:
        self.refresh()
        self.root.after(POLL_MS, self.tick)


_mutex = None  # kept for the life of the process; Windows frees it on exit


def _claim_single_instance() -> bool:
    """Take the panel mutex; False if another panel already holds it."""
    global _mutex
    if sys.platform != "win32":
        return True
    import ctypes
    k32 = ctypes.windll.kernel32
    k32.CreateMutexW.restype = ctypes.c_void_p
    _mutex = k32.CreateMutexW(None, False, estop.PANEL_MUTEX)
    return k32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-topmost", action="store_true")
    ap.add_argument("--titlebar", action="store_true", help="keep a normal window frame")
    args = ap.parse_args()
    if not _claim_single_instance():
        print("a STOP panel is already open", file=sys.stderr)
        return
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"no display available for the e-stop panel ({exc}). "
              f"Use: python -m nis_elements_mcp.estop engage", file=sys.stderr)
        raise SystemExit(1)
    Panel(root, topmost=not args.no_topmost, borderless=not args.titlebar)
    root.mainloop()


if __name__ == "__main__":
    main()
