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
| `imaging_status` | active saved optical configuration names and their raw return value, separate objective/camera name readbacks and diagnostic errors, light path and shutter states (read-only) |
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

## Hardware validation results (2026-11-17)

These checks used the real MCP server in NIS. Moves and objective changes
were requested by the operator; the 500 um tests included an observation
pause. Results below are based on tool responses and position readbacks,
not independent measurement of physical travel or image displacement.
No screen automation was used. No real capture or ND experiment was
started during these hardware checks.

### Read-only checks

| Check | Result | Observed details |
|---|---|---|
| `echo` | Passed | Live MCP connection answered |
| `status` | Passed | Reported XY/Z, objective, E-stop, ND state and configured limits |
| `capture_status` | Passed | Idle, no capture in progress; also remained idle after the blocked capture request |
| `imaging_status` | Responded; incomplete imaging information | Optical configuration fields were blank; light path port 3 of 4 had no mapped name; DIA LED, AX and D-LEDI shutters reported closed. Camera/live-view state is not exposed |
| `list_optical_configs` | Passed | Returned 14 saved configurations; none was selected by the tool |
| `list_experiments` | Passed | Returned 7 saved experiments and their configured time-lapse settings; none was run |

### Stage and objective checks

Starting XY for these tests was **X -3406.3 um, Y -3142.5 um**. Each
successful move reported `rc: 1`. The XY and objective checks did not
request Z movement; a separate downward focus check is recorded below.

| Test | Result | Position readback / objective |
|---|---|---|
| +10 um X, then return | Passed | X -3406.3 -> -3396.3 -> -3406.3 um; Y unchanged |
| +10 um Y, then return | Passed | Y -3142.5 -> -3132.5 -> -3142.5 um; X unchanged |
| +500 um Y, pause 10 s, then return | Passed | Y -3142.5 -> -2642.5 -> -3142.5 um; X unchanged |
| +500 um X, pause 10 s, then return | Passed | X -3406.3 -> -2906.3 -> -3406.3 um; Y unchanged |
| 4x -> 10x, pause 10 s, then 4x | Passed for nosepiece selection | Positions 0 -> 1 -> 0; both changes reported `changed: true`, `rc: 1`. Calibration calls returned `rc_calibration: 0`; optical calibration accuracy was not independently checked |
| Apply recorded XY endpoints | Passed for configuration loading | Repo updated to X +/-57047.0 um, Y +/-37547.0 um; live `status` confirmed the same limits |
| `focus_down` by 10 um after manual E-stop release | Passed | Starting Z 2584.90 um; immediate `z_after_um` 2574.92 um; follow-up `status` Z 2574.84 um; `rc: 1`. XY unchanged; 4x remained selected |
| Manual Z restoration after focus test | Passed by status readback | Operator restored Z manually; subsequent `status` confirmed 2584.90 um, matching the recorded starting Z. XY remained (-3406.3, -3142.5) um; 4x selected |

For the successful focus test, the **manual restore target is Z 2584.90 um**.
The two final readings were 2574.92 um immediately after the command and
2574.84 um on the following status query. The operator subsequently
restored Z manually, and a fresh `status` confirmed **2584.90 um**, matching
the restore target. No automated upward restoration was commanded.

The XY endpoint readings came from earlier manual stage movement on the
same date. Applying them replaced an older temporary area of
X 14024.8..16024.8 um and Y -1712.9..287.1 um. The current sample position
was outside that old area; no automated move was made until the requested
limit update and the operator's manual E-stop release.

### Software E-stop checks

Each request below was made with the E-stop engaged. A refusal is the
expected passing result, even though the MCP response has `isError: true`.

| Request while stopped | Result | Verification |
|---|---|---|
| `move_relative` +10 um X | Passed: move refused | XY remained (-3406.3, -3142.5) um |
| `change_objective` to 10x | Passed: objective change refused | Status still reported 4x, nosepiece position 0 |
| `capture` | Passed: capture refused | `capture_status` stayed idle with no capture in progress; acquisition was not started |
| `run_experiment` for `Celegans_01_stack_timing` | Passed: ND run refused | Status reported `nd_running: false`; the saved experiment is configured for 90 s, 5 frames at 20 s intervals |
| `focus_down` by 10 um | Passed: focus refused | Z readback was 2584.90 um before and after; no Z move was issued |

After the E-stop refusal checks, the operator manually released the E-stop
for the successful focus test and the restriction checks below.
At the last recorded status after restoring the XY limits following the
boundary checks, the software
E-stop was **released**, the objective was **4x**, XY was
**(-3406.3, -3142.5) um**, and Z was **2584.90 um**. The earlier manual
restoration readback was 2584.90 um. No ND experiment was running. This is a dated
record, not current instrument state; call `status` before further work.
The E-stop is a software command interlock, not validation of a physical
emergency-stop circuit or cancellation of an already-running native call.
Only a person releases it.

### Restriction checks with E-stop released

These requests reached the live server with the E-stop released, so the
refusals below verify the individual restrictions rather than the E-stop
interlock. Status was read before and after each request.

| Request | Result | Verification |
|---|---|---|
| `move_relative` +1001 um X, 0 um Y | Passed: maximum step exceeded | Returned `steps over 1000 um per axis are refused, got (1001, 0)`; XY remained (-3406.3, -3142.5) um; no movement occurred |
| `move_relative` 0 um X, +1001 um Y | Passed: maximum step exceeded | Returned `steps over 1000 um per axis are refused, got (0, 1001)`; XY remained (-3406.3, -3142.5) um; no movement occurred |
| `change_objective` to `20x` | Passed: objective restricted | Returned `only 4x/10x objectives are allowed, got '20x'`; 4x and nosepiece position 0 remained selected; no nosepiece movement occurred |
| `run_experiment` for `Celegans_02_pilot_short_60s` | Passed: 30-minute duration limit | `list_experiments` confirmed 3600 s (60 min), 61 frames at 60 s intervals. The server refused it as exceeding `MAX_ND_RUN_S` (30 min); status remained `nd_running: false`; no experiment started |
| `focus_down` by 201 um | Passed: 200 um maximum exceeded | Returned `down_um must be above 0 and at most 200 um, got 201`; status before and after reported Z 2584.94 um and XY (-3406.3, -3142.5) um, with 4x selected and E-stop released. No hardware moved |

For the first four checks, XY remained **(-3406.3, -3142.5) um**, Z read
**2584.92 um**, and the objective remained **4x**. The experiment's `60s`
name suffix describes its interval, not its total duration.
The later 201 um focus-limit check also passed without movement; its Z
readback remained **2584.94 um** before and after the refusal.

### Configured XY boundary rejection

Both positive-axis boundary checks passed on the live server with the
E-stop released. The operator confirmed clearance for a 10 um move before
the test. A temporary software rectangle was placed +/-5 um around the
starting XY; the stage was not moved to a physical travel edge.

| Configuration / readback | X (um) | Y (um) |
|---|---|---|
| Starting position | -3406.3 | -3142.5 |
| Temporary minimum | -3411.3 | -3147.5 |
| Temporary maximum | -3401.3 | -3137.5 |
| Final position after both refusals and restoration | -3406.3 | -3142.5 |
| Restored original minimum | -57047.0 | -37547.0 |
| Restored original maximum | +57047.0 | +37547.0 |

The live `status` response confirmed the temporary limits before either
request was submitted.

| Request | Result | Rejected target / position verification |
|---|---|---|
| +10 um X, 0 um Y | Passed: outside safe XY area | Target (-3396.3, -3142.5) um exceeded temporary X maximum -3401.3 um; reported position and follow-up status remained (-3406.3, -3142.5) um |
| 0 um X, +10 um Y | Passed: outside safe XY area | Target (-3406.3, -3132.5) um exceeded temporary Y maximum -3137.5 um; reported position and final status remained (-3406.3, -3142.5) um |

These 10 um requests were below the 1000 um maximum step, so their
refusals specifically exercised `XY_LIMITS_UM`. No stage movement occurred.
Afterward, the original limits were restored in the repo and confirmed by
the live server: X +/-57047.0 um, Y +/-37547.0 um. The entire source file
matched its pre-test backup byte for byte. Final status reported 4x,
Z 2584.90 um, no ND run, and E-stop released.

Tool-call evidence is in `~/nis-mcp/log.jsonl` (on this PC,
`C:\Users\AX Confocal\nis-mcp\log.jsonl`), including refused requests.

### Mock validation and remaining work

Latest repo run: `Run-MockTests.ps1` on **2026-11-17 at 13:56 local time**
reported **59 passed, 1 excluded** (`1 deselected` in pytest output), with
**exit code 0**. The excluded test reads actual NIS experiment settings.
The saved JUnit report confirms 59 tests, zero failures and zero errors.
NIS and the live MCP server were untouched; no real capture or experiment
started.

Saved report:

```text
D:\QurratulAin_ConfocalOrchestratorProject\Confocal_MCP\nis-mcp-server\test-reports\mock-20261117-135615-b5a0e12c\results.xml
```

Saved console log:

```text
D:\QurratulAin_ConfocalOrchestratorProject\Confocal_MCP\nis-mcp-server\test-reports\mock-20261117-135615-b5a0e12c\pytest.log
```

After the 0.8.4 E-stop fixes, the full isolated mock-only run passed
**63 tests**, with **1 deselected**, and exit code **0**. Its saved report
and console log are under:

```text
C:\Users\AX Confocal\Documents\Codex\2026-10-30\ho\outputs\estop-fix-mock-reports\mock-20261117-141945-ea099fde\results.xml
C:\Users\AX Confocal\Documents\Codex\2026-10-30\ho\outputs\estop-fix-mock-reports\mock-20261117-141945-ea099fde\pytest.log
```

The 1 test remains deselected
because it reads this PC's actual saved NIS experiment settings. Successful
and failed captures over isolated HTTP both verified progress, phase
timings, terminal results and logs. After the XY limit update, the 3
movement/E-stop mock tests also passed. Use `Run-MockTests.ps1` below to
rerun the full suite and save reports.

| Still needs testing | Scope / prerequisite |
|---|---|
| Non-laser camera acquisition | Await the NIS-integrated camera. Set up a compatible transmitted-light brightfield/DIC camera path and verify a useful live image before a real capture |
| Real capture with the updated code | Validate progress polling, phase timings, NIS responsiveness, ND2 readability, image quality and document handling on actual acquisition. Mock success does not establish these |
| Optical configuration selection verification | Pending: the operator sees no optical configuration button highlighted, so blank active configuration names appear expected. Selecting a known configuration and checking its reported names is deferred because selection changes settings; no configuration was applied for this investigation |
| Imaging-state reporting | Camera-name binding via `g5_new_ml.dll` passed a live read-only check, returning `Nikon_Confocal_Ax`. Light-path port-name mapping and live-view reporting remain pending |
| ND run and finish behavior | Successful runs, `finish_experiment`, busy-state reporting and actual timing remain unvalidated in this session. Do not run automatically as part of safety tests |
| Geometry and accuracy | Confirm clearance for the current holder/sample/objective and validate calibration, repeatability and image displacement as needed; endpoint readings and software readbacks alone do not establish these |

## E-stop preflight checks (0.8.4)

`focus_down` rechecks the E-stop after acquiring the NIS lock and reading
the starting Z, immediately before issuing `StgMoveZ`. This prevents a
pending focus request from proceeding after a stop engaged during its
lock wait or position read.

Capture rechecks the E-stop immediately before `Capture()`, after imaging
reads and acquisition-phase logging. A stop during either preflight step
produces a terminal `cancelled` result without calling capture, save, or
close. The phase timing is finalized before that result is stored, so
`capture_status` and the log retain consistent cancellation timings.
An `acquire` phase event can therefore exist even when E-stop prevented
the actual native acquisition call.

Four new mock regression cases cover focus lock-wait and position-read
stops, plus capture imaging-read and acquisition-phase-start stops. All
four failed against 0.8.3 and passed with the fix. They verify no native
mutation, lock release, and, for capture, cached cancellation results,
phase timing logs, and absence of an output file.

CI now runs both `test_nis_mcp.py` and `test_capture_mock_http.py`, excluding
the actual-settings-file test, on the existing Windows/Linux and Python
3.11/3.13 matrix. Local mock validation does not establish remote CI status
or hardware behavior. These checks do not cancel a native operation that
has already started; a physical emergency stop remains separate.

## Imaging diagnostics (0.8.3)

`imaging_status` retains the existing `optical_config`, `light_path`,
`shutters` and `estop_engaged` fields. It adds these read-only diagnostics:

| Field | Meaning |
|---|---|
| `objective` | Current objective name from `GetCurrentObjName`; `null` if empty |
| `objective_rc` | Raw return value from that name query; `null` if the call raises an exception |
| `camera_name` | Currently selected camera name from `GetCurrentCameraName`; `null` if empty or unavailable |
| `camera_name_rc` | Raw return value from that camera query; `null` if the call raises an exception |
| `optical_config_rc` | Raw return value from `GetSelectedOptConf`; `null` if the call raises an exception |
| `diagnostic_errors` | Present when a name query raises an exception; maps function names to error details. Other readbacks remain available |

`optical_config.objective`, `.camera`, and `.microscope` are **active saved
configuration names** associated with those settings categories. They are
not hardware names. Blank configuration names do not mean there is no
objective or camera. The actual objective and selected camera name are
queried separately. A camera name does not prove that it is connected,
ready, or delivering frames. Live-view state is not queried.

The return values are exposed without assigning undocumented success or
failure meanings; for example, a zero return is not automatically treated
as failure. A missing camera-name DLL export is optional and does not
disable existing tools. The installed NIS 6.20 macro reference documents
`GetSelectedOptConf` under OpticalConfiguration and `GetCurrentCameraName`
under Camera - Other.

Version 0.8.3 binds the camera getter from `g5_new_ml.dll`, where the
installed x64 DLL exports `?GetCurrentCameraName@@YAHPEA_W@Z` (C++ linkage
for `int GetCurrentCameraName(wchar_t*)`). The binding uses a writable
Unicode buffer and exposes the raw integer return value. It is resolved
on the first camera-name read and cached; a missing DLL/export is reported
through `diagnostic_errors` without disabling other imaging readbacks.
It does not call the unverified `GetCurrentCameraName_Impl` export.

These diagnostics do not select configurations, open shutters, move
hardware, start live imaging, capture images, or run experiments. They are
also included in capture imaging metadata because capture uses the same
readback helper. During an active capture, `imaging_status` continues to
return cached capture progress rather than enter NIS.

Mock verification includes separate hardware/configuration names, blank
configuration and camera names, raw return values of 0/1/-4, and isolated
read exceptions for each query. Additional binding tests use a fake DLL
loader to verify the exact export, signature, lazy resolution, caching,
and missing DLL/export errors. The full mock-only suite passed 59 tests
with the actual-settings-file test deselected before repo integration.

The earlier 0.8.2 read-only live check reported objective
`PLAN APO \u03bbD 4x OFN25`, `objective_rc: 0`, blank configuration names,
and `optical_config_rc: 0`. `camera_name` and `camera_name_rc` were `null`,
with `diagnostic_errors.GetCurrentCameraName` reporting
`AttributeError: function 'GetCurrentCameraName' not found`. Thus the
installed macro reference documents the function, but `g5_regprocs.dll`
does not export it. Offline export inspection subsequently located it in
`g5_new_ml.dll`, leading to the 0.8.3 binding above. The original failure
was an API binding issue, not evidence that a camera
is disconnected. No fallback function, camera selection, capture, or
experiment was invoked.

The 0.8.3 read-only check on 2026-11-17 returned camera name
`Nikon_Confocal_Ax`, `camera_name_rc: 0`, objective
`PLAN APO \u03bbD 4x OFN25`, and `objective_rc: 0`, with no
`diagnostic_errors`. Saved configuration names remained blank with
`optical_config_rc: 0`; all reported shutters remained closed. This
confirms the camera-name binding works for the currently selected NIS
device, not that a brightfield camera is connected or delivering an image.
No camera selection, capture, or experiment was started.

The operator subsequently confirmed that **no optical configuration button
is highlighted in NIS**. Together with the read-only API returning blank
active configuration names while 14 saved configurations remain listed,
this supports the interpretation that no saved configuration is selected.
Blank names therefore **appear expected in the observed state**, rather
than establishing a reporting fault. This remains an interpretation of
the API output and the operator's observation, not a selection test.

**Selection verification is pending.** Applying a known configuration and
checking whether its names appear in `imaging_status` would change NIS
settings, so it was not performed. The configuration's included settings
and NIS's behavior when configuration settings change have not yet been
confirmed. No settings were changed and no capture or experiment was
started for this investigation.

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
setup problem. The last verified full run reported **63 passed, 1
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
