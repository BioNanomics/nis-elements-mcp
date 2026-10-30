# `nis-mcp-bridge job-file`: where the file to paste into NIS is. Works with no bridge running.
import ast
import sys

from nis_elements_mcp import nis_bridge_client as client_mod


def test_job_file_prints_the_bridge_job_path(monkeypatch, capsys):
    monkeypatch.setenv("NIS_BRIDGE_PORT", "9")                 # nothing listens there: not needed
    monkeypatch.setattr(sys, "argv", ["nis-mcp-bridge", "job-file"])
    client_mod.main()
    path = capsys.readouterr().out.strip()
    assert path.endswith("nis_bridge_job.py") and path == str(client_mod.job_file())


def test_the_job_file_imports_only_the_standard_library():
    # NIS's own Python has nothing else installed, so the pasted file must stand alone.
    tree = ast.parse(client_mod.job_file().read_text(encoding="utf-8"))
    imported = {(n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    imported |= {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    allowed = set(sys.stdlib_module_names) | {"__future__", "limjob"}       # limjob is NIS's own
    assert imported <= allowed, imported - allowed
