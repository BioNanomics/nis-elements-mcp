"""Replay successful/failed captures over isolated HTTP, with no real NIS access."""
import json
import threading
import time

import NISMCPServer as server
from test_nis_mcp import main_thread, no_real_log, post, scope, url


def _exercise_capture(url, scope, main_thread, monkeypatch, tmp_path, *, fail):
    # Imported fixtures supply a fake DLL, fake UI loop, temporary files and an
    # ephemeral HTTP port. no_real_log also rejects real NIS initialization.
    monkeypatch.setattr(server, "CAPTURE_RESPONSE_WAIT_S", 0.02)
    acquire_started, release_acquire = threading.Event(), threading.Event()
    save_started, release_save = threading.Event(), threading.Event()
    trace = []
    failure = "RuntimeError: simulated acquisition failure: mock device disconnected"
    original_capture, original_save = scope.Capture, scope.ImageSaveAs

    def mock_capture():
        original_capture()
        acquire_started.set()
        assert release_acquire.wait(3), "mock acquisition gate timed out"
        if fail:
            raise RuntimeError(failure.removeprefix("RuntimeError: "))
        return 0

    def mock_save(*args):
        save_started.set()
        assert release_save.wait(3), "mock save gate timed out"
        return original_save(*args)

    scope.Capture, scope.ImageSaveAs = mock_capture, mock_save

    def rpc(name, arguments):
        began = time.monotonic()
        code, response = post(url + "/mcp", {
            "jsonrpc": "2.0", "id": len(trace) + 1, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}})
        assert code == 200 and not response["result"]["isError"]
        payload = json.loads(response["result"]["content"][0]["text"])
        trace.append({"tool": name, "response_s": round(time.monotonic() - began, 6),
                      "isError": response["result"]["isError"], "response": payload})
        return payload

    try:
        pending = rpc("capture", {"filename": "MOCK_ONLY_failed" if fail else "MOCK_ONLY_success"})
        assert pending["capture_in_progress"] and acquire_started.wait(1)
        capture_id = pending["capture_id"]
        progress = rpc("capture_status", {"capture_id": capture_id})
        assert progress["state"] == "running" and progress["phase"] == "acquire"
        assert server._nis.lock.locked()
        release_acquire.set()
        if not fail:
            assert save_started.wait(1)
            progress = rpc("capture_status", {"capture_id": capture_id})
            assert progress["state"] == "running" and progress["phase"] == "save"
            release_save.set()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            final = rpc("capture_status", {"capture_id": capture_id})
            if not final["capture_in_progress"]:
                break
            time.sleep(0.01)
        else:
            raise AssertionError("mock capture did not finish")
    finally:
        release_acquire.set()
        release_save.set()

    expected = "failed" if fail else "completed"
    result = final["result"]
    assert final["state"] == final["phase"] == result["state"] == expected
    assert result["capture_id"] == capture_id
    assert result["saved"] is (not fail)
    assert not server._nis.lock.locked()
    assert rpc("capture_status", {"capture_id": capture_id})["result"] == result
    assert all(value >= 0 for value in result["timings_s"].values())
    assert result["total_s"] >= max(result["timings_s"].values())

    entries = [json.loads(line) for line in server.LOG_FILE.read_text(encoding="utf-8").splitlines()]
    events = [entry for entry in entries if "event" in entry]
    assert all(entry["capture_id"] == capture_id for entry in events)
    assert events[0]["event"] == "capture_queued"
    terminal = [entry for entry in events if entry["event"] == "capture_finished"]
    assert len(terminal) == 1 and terminal[0]["state"] == expected
    assert terminal[0]["total_s"] == result["total_s"]
    phases = [entry["phase"] for entry in events if entry["event"] == "capture_phase_started"]
    assert phases == (["wait_nis", "read_imaging", "acquire"] if fail else
                      ["wait_nis", "read_imaging", "acquire", "save", "close"])
    finished = {entry["phase"]: entry["took_s"] for entry in events
                if entry["event"] == "capture_phase_finished"}
    assert all(finished[phase] == result["timings_s"][phase] for phase in phases)
    errors = [entry for entry in events if entry["event"] == "capture_phase_error"]
    if fail:
        assert result["failed_phase"] == "acquire" and result["error"] == failure
        assert len(errors) == 1 and errors[0]["phase"] == "acquire"
        assert errors[0]["error"] == terminal[0]["error"] == failure
        assert [call[0] for call in scope.calls] == ["capture"]
        assert not server.Path(final["path"]).exists()
    else:
        assert not errors and "error" not in result and result["statistics_skipped"]
        assert [call[0] for call in scope.calls] == ["capture", "save", "close"]
        assert server.Path(final["path"]).read_bytes() == b"mock ND2 data"

    (tmp_path / "progress.json").write_text(json.dumps({
        "mock_only": True, "endpoint": url + "/mcp", "observations": trace}, indent=2), encoding="utf-8")
    print(f"\nMock {expected}: {result['timings_s']}; total_s={result['total_s']}")
    print(f"Progress: {tmp_path / 'progress.json'}\nTiming log: {server.LOG_FILE}")


def test_successful_capture_progress_and_timing_logs(url, scope, main_thread, monkeypatch, tmp_path):
    _exercise_capture(url, scope, main_thread, monkeypatch, tmp_path, fail=False)


def test_failed_capture_progress_and_timing_logs(url, scope, main_thread, monkeypatch, tmp_path):
    _exercise_capture(url, scope, main_thread, monkeypatch, tmp_path, fail=True)
