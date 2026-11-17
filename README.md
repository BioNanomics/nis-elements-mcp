# nis-mcp-server

An MCP server that runs **inside NIS-Elements**, as a JOBS Python task, and
serves MCP over HTTP on `http://127.0.0.1:8767/mcp`. Nothing to install on
the PC besides NIS: the server uses only the standard library.

## Tools

| Tool | What it does |
|---|---|
| `echo` | returns the text: checks that the server answers |
| `status` | stage XY and Z, objective, nosepiece, ND run state, e-stop state, safe XY area (read-only) |
| `move_relative` | XY move from where the stage is, at most 1000 um per axis, never out of the safe XY area |
| `focus_down` | lowers the objective (away from the sample) by at most 200 um; nothing raises it: a person does, watching the gap |
| `change_objective` | turns the nosepiece and switches NIS's calibration; 4x and 10x only |
| `list_optical_configs` | the optical configurations defined in NIS (read-only; select one in NIS) |
| `imaging_status` | the active optical configuration (objective, camera and microscope parts), the light path port and each shutter's state (read-only) |
| `capture` | one image with the active optical configuration, saved as ND2 in `~/nis-mcp/captures`, by plain file name, never overwriting. Returns promptly with a capture ID and queued/running state for a slow capture; fast captures can return their completed result. Per-phase timings are included. `include_stats=true` opts into sampled intensity statistics; they are skipped by default |
| `capture_status` | cached progress/result for the current or last capture, optionally checked against `capture_id`; never enters NIS or waits for its UI |
| `list_experiments` | the ND experiments saved in NIS, with each one's loops, time-lapse length (or "single pass"), interval/duration/frames, Z steps and output file (read-only) |
| `run_experiment` | runs an ND experiment saved in NIS's ND Acquisition window, by name; a name that is not saved, or a time-lapse over 30 min, is refused |
| `finish_experiment` | ends the running ND experiment after its current loop |
| `estop` | `engage` creates the e-stop file `~/.nis-mcp/ESTOP`; `status` reports it |

While the e-stop file exists, every move, objective change, capture and ND
run is refused. Only a person releases it, by deleting the file:
```powershell
Remove-Item ~\.nis-mcp\ESTOP
```
`finish_experiment` and `estop` always work: they only stop things.

The limits are constants at the top of `NISMCPServer.py`: edit and save.

| Constant | What it limits |
|---|---|
| `XY_LIMITS_UM` | the XY area the stage may not leave, `((x_min, x_max), (y_min, y_max))` in um as `status` reports it; `None` refuses every move. Current recorded travel endpoints: X -57047.0 to +57047.0 um, Y -37547.0 to +37547.0 um; applied at the operator's request on 2026-11-17. These endpoints do not establish sample/objective clearance throughout the rectangle; reassess when the setup changes |
| `MAX_STEP_UM` | the largest XY step per call (1000 um) |
| `ALLOWED_MAGNIFICATIONS` | the objectives that can be selected (4x, 10x) |
| `MAX_ND_RUN_S` | the longest time-lapse `run_experiment` starts (30 min); start longer runs in NIS |
| `ALLOWED_EXPERIMENTS` | if it lists names, only those ND experiments run |
| `CAPTURE_DIR` | where captures go (`~/nis-mcp/captures`) |
| `LOW_SIGNAL_FRACTION`, `SATURATED_FRACTION` | when capture warns (1% of full scale; 1% of pixels saturated) |
| `LIGHT_PATH_NAMES` | names for the light path port numbers `imaging_status` reports; fill in on the scope |

Every tool call is logged as one JSON line in `~/nis-mcp/log.jsonl`: time,
tool, arguments, result, refusals included.

## Capture progress (0.8.1)

Call `capture` with `{"filename": "sample_check"}`. A fast operation returns
`state: "completed"` and `saved: true`. A slow operation returns within about
one second with `capture_id`, `capture_in_progress: true`, and its current
`phase`. This is pending, not success, failure, or cancellation. Do not retry
the capture. Poll `capture_status` with that ID until `capture_in_progress`
is false; the terminal `result` contains the saved path or error. Only the
current/last capture ID is retained, so retrieve it before starting another.

Phases are `queued`, `wait_nis`, `read_imaging`, `acquire`, `save`, optional `statistics`,
and `close`, followed by `completed`, `failed`, or `cancelled`. Timings use a
monotonic clock. The log also appends timestamped `capture_queued`,
`capture_phase_started`, `capture_phase_finished`, `capture_phase_error`,
and `capture_finished` records, keyed by `capture_id`. A completion record
is written even if the original HTTP client has stopped waiting.

While queued/running, native NIS tools (including duplicate captures) are
refused. `status` and `imaging_status` return cached capture progress rather
than entering NIS; they do not claim to read current XYZ/imaging settings.
`capture_status`, `echo`, `estop`, and file-based `list_experiments` remain
available. A capture queued longer than 30 seconds expires and cannot start
late. Once native acquisition starts it is **not cancelled** by an HTTP
timeout, disconnect, or E-stop. E-stop blocks new acquisitions; a frame
already acquired may still be saved and closed to preserve its data.

NIS acquisition remains on its UI thread. A long quality-mode confocal scan
can still make NIS itself unresponsive: this change provides observable
progress and prevents retries; it does not introduce an asynchronous Nikon
acquisition engine or shorten the saved acquisition settings. All optical
settings, camera choice, laser settings, XYZ, and limits are left unchanged.

Statistics are now opt-in because a 64 x 64 grid requires 4096 native pixel
reads on the UI thread. Use `include_stats: true` only when needed; preferably
analyze the saved ND2 outside NIS. Nonempty output is checked before closing
the image. A nonempty file does not establish readability or useful signal.
Native return-code meanings and document identity are not fully validated;
do not use this change as proof that a capture is safe for an unknown setup.

Auto-reload is deferred while a capture is queued/running so an executing
operation cannot lose its reservation or progress state.

### Mock-only verification

Run these commands in a normal PowerShell terminal, outside NIS. NIS does
not need to be open, and the MCPServer job does not need to be restarted.
The mock tests do not connect to NIS, use lasers, move hardware, or capture
a real image.

First, open the repo and install the test dependency once with a standalone
Python 3.11+ interpreter:

```powershell
Set-Location 'D:\QurratulAin_ConfocalOrchestratorProject\Confocal_MCP\nis-mcp-server'
python --version
python -m pip install pytest
```

Run the full mock-only suite and save reports:

```powershell
.\Run-MockTests.ps1
```

To run it from any other directory:

```powershell
& 'D:\QurratulAin_ConfocalOrchestratorProject\Confocal_MCP\nis-mcp-server\Run-MockTests.ps1'
```

The runner uses standalone `python` (Python 3.11+ with `pytest` installed)
and works from any current directory when called by its full path. It runs
both test files and excludes `test_reads_this_pcs_saved_experiments`. Each
run saves `results.xml` (JUnit), `pytest.log`, and temporary mock artifacts
under a unique `test-reports/mock-...` directory and prints their paths.
The report directory is ignored by Git. It returns pytest's exit code,
including a nonzero code on failure, and does not install dependencies or
start, stop, or contact the live MCP server.

To choose a Python executable or another report directory:

```powershell
.\Run-MockTests.ps1 -Python 'C:\path\to\python.exe' -ReportDirectory 'D:\MockReports'
```

To run the full suite directly without the report-saving wrapper:

```powershell
python -m pytest -q -ra test_nis_mcp.py test_capture_mock_http.py -k "not reads_this_pcs_saved_experiments"
```

The fake-scope tests refuse any attempt to instantiate the real NIS DLL.
HTTP tests bind ephemeral ports, and reload tests use a fake UI hook; they
do not contact the real MCP endpoint. The optional SDK test is skipped when
its dependencies are absent. The excluded test reads this PC's actual ND
settings file and is not part of the mock-only suite.

To replay just the successful and failed captures through the MCP HTTP
interface:

```powershell
python -m pytest -q -s test_capture_mock_http.py
```

Run only the successful capture mock:

```powershell
python -m pytest -q -s test_capture_mock_http.py::test_successful_capture_progress_and_timing_logs
```

Run only the failed capture mock:

```powershell
python -m pytest -q -s test_capture_mock_http.py::test_failed_capture_progress_and_timing_logs
```

These two tests use a fake microscope, a fake UI loop, temporary capture/log
files, and a separate HTTP server on an automatically assigned local port.
They do not contact the live MCP server or require NIS to be open. They
reject real NIS DLL initialization. The successful test checks progress
through acquisition and save; the failed test injects an acquisition error
and checks that no save or close occurs. Both verify the final
`capture_status`, phase durations, terminal log event, and lock release.
The failure is reported inside `capture_status.result.error`; the status
query itself succeeds.

With `-s`, each test prints its timing summary and the paths to
`progress.json` (HTTP responses) and `log.jsonl` (phase events). These are
pytest temporary files; copy them elsewhere if you want to keep them across
future runs. Saved mock ND2 bytes are test data, not a real image.

The PowerShell runner retains those artifacts under its own report folder:

```text
test-reports/
  mock-<timestamp>-<unique-id>/
    results.xml                  JUnit test results
    pytest.log                   Console output and timing summaries
    test-data/
      test_successful_capture.../
        progress.json            Capture and capture_status responses
        log.jsonl                Phase timing events
      test_failed_capture.../
        progress.json            Failed operation and error details
        log.jsonl                Error event and phase timings
```

The suite also creates temporary files for its other mocked tests under
`test-data`. For automation, run the script in a child PowerShell process
and inspect its exit code:

```powershell
powershell -NoProfile -File .\Run-MockTests.ps1
$LASTEXITCODE
```

Exit code `0` means the tests passed; a nonzero code means a failure or
setup problem. The last verified full run reported **59 passed, 1
deselected**; the deselected test reads actual NIS experiment settings.
If optional MCP SDK dependencies are absent, that client test is skipped.

On connect the server sends instructions telling the agent to check the
scope's state with a tool rather than remember it: a person can move the
stage or release the e-stop at any time.

Capture and the ND calls open NIS windows, which crashed NIS when called
from any thread but NIS's main one. So they run on the main thread, through
a timer the `MCPServer` job installs there; without it (the server started
outside NIS) they are refused.

This project is not affiliated with, endorsed by, or supported by Nikon.
It contains no Nikon code: the server calls NIS-Elements' own macro
functions on a PC where NIS-Elements is already installed and licensed.
Nikon and NIS-Elements are trademarks of Nikon Corporation.

| File | What it is |
|---|---|
| `NISMCPServer.py` | the server |
| `install_job.py` | creates the `MCPServer` job in NIS's JOBS database; the job just loads `NISMCPServer.py` from this folder |
| `test_nis_mcp.py` | tests (`pytest`; one test uses the `mcp` SDK if installed) |
| `test_capture_mock_http.py` | repeatable successful/failed capture progress and timing tests, using mocks over HTTP |
| `Run-MockTests.ps1` | runs the full mock-only suite and saves reports and mock artifacts |

## Install

1. Find the JOBS database NIS uses (here `D:\Embryos\Embryos.dat`) and run,
   with any Python 3.11+ (NIS's own works):
   ```powershell
   & "C:\Program Files\NIS-Elements\Python\python.exe" install_job.py --db D:\Embryos\Embryos.dat
   ```
   It backs the database up next to it first. Run it again to update the job.
2. In NIS, reopen JOBS (or restart NIS) and run the `MCPServer` job. The job
   finishes at once; the server keeps running.
3. Register it with Claude Code, once:
   ```powershell
   claude mcp add --transport http nis http://127.0.0.1:8767/mcp
   ```

## Upgrade

Edit and save `NISMCPServer.py`. The running server notices within a
couple of seconds and replaces itself with the new code; if the new code
does not load (a syntax error, say), the old server keeps running and the
error is printed in the JOBS log. Running the `MCPServer` job again also
restarts it. No copy-paste into NIS.

## Stop

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8767/shutdown
```

Never press the JOBS **Abort** button: it freezes NIS.

## Known limits

1. **An ND run holds NIS until it ends.** `run_experiment` answers only when
   the run is over. Meanwhile `status` answers at once from the server's own
   record (experiment, start, expected end, elapsed time, XY at the start)
   and `estop` works, but the tools that need NIS answer that it is busy, so
   `finish_experiment` cannot end it early. Use NIS's **Finish** button in
   the ND Progress window. NIS also keeps each run's ND2 file open
   afterwards, so it cannot be deleted until it is closed in NIS.
2. **The saved ND experiments are read from NIS's own file**,
   `C:\ProgramData\Laboratory Imaging\Platform\AX Confocal\ND_Stored.bin`,
   because no macro function lists them. NIS itself does not say whether a
   name was found (it runs whatever the ND Acquisition window holds), so
   `run_experiment` refuses any name not in that file, and refuses
   everything if the file cannot be read. The format is undocumented: a NIS
   update could change it, and then `test_reads_this_pcs_saved_experiments`
   fails. NIS may write the file only when it saves its settings, so an
   experiment saved a moment ago may not be listed yet. To narrow the
   choice further, list names in `ALLOWED_EXPERIMENTS`.
3. **The optical configuration cannot be selected** from here: choose it in
   NIS before `capture`.
4. **Updating from 0.3.0 by saving the file** leaves capture and the ND
   tools without the main-thread hook (0.3.0 reloads on its watcher thread).
   Run the `MCPServer` job once more; later saves keep the hook.

## How the installer works

NIS keeps jobs in SQLite: a `jobdef` row per job and a `jobprogram` row per
save, whose task list holds each task's settings as base64 of NIS's binary
record format. `install_job.py` copies an existing one-Python-task job and
swaps in a loader script. The format is undocumented, worked out from NIS
6.20's database on this PC; a NIS update could change it.
