# nis-elements-mcp

Drive a Nikon microscope through **NIS-Elements** from an MCP client such as
Claude Code: read the stage and objective, move XY, change objective, capture
an image, and run ND experiments you saved in NIS, with an emergency stop and
hard safety limits.

NIS-Elements has no outside automation interface, so a small **bridge job**
runs inside NIS (as a JOBS Python task) and answers requests on
`127.0.0.1:8766`. The MCP server talks to that bridge.

```
MCP client (Claude Code)
   │  stdio
   ▼
nis-mcp                 MCP server: confirm gate, limits, e-stop, event log
   │  HTTP, 127.0.0.1:8766
   ▼
nis_bridge_job.py       JOBS Python task, runs inside nis_ar.exe
   │  ctypes
   ▼
NIS macro functions     StgMoveXY, Capture, ND_RunExperiment, ...
```

Tested on NIS-Elements AR with a Nikon Ti2 stand and an AX confocal (status,
optical configurations, XY moves, objective changes and capture). ND runs are
implemented and covered by tests but not yet checked on a microscope.

## Requirements

- Windows, with NIS-Elements and its **JOBS** module (Python task).
- Python 3.11 or newer on the same PC, for the MCP server.
- The bridge job itself needs nothing installed inside NIS: it uses only the
  Python standard library.

## Install into a project

From the project folder, in PowerShell:

```powershell
python -m venv .nis-mcp-env
.nis-mcp-env\Scripts\pip install "git+https://github.com/BioNanomics/microscope-agent#subdirectory=packages/nis-elements-mcp"
claude mcp add nis --scope local -- "$PWD\.nis-mcp-env\Scripts\nis-mcp.exe"
```

- If `python -m venv` or `pip install` fails with "The filename or extension
  is too long" (WinError 206), the project folder's path is too deep for
  Windows' 260-character limit: use a shorter folder, or turn on long paths
  in Windows.
- Instead of the git URL you can give a local path, e.g.
  `.nis-mcp-env\Scripts\pip install D:\path\to\microscope-agent\packages\nis-elements-mcp`.
- The full path (`$PWD\...`) makes the server start whatever folder Claude is
  launched from.
- Pass settings with `-e`, before the `--`:
  ```powershell
  claude mcp add nis --scope local -e NIS_CAPTURE_DIR=D:\data\nis\captures -e NIS_LOG_DIR=D:\data\nis\logs -- "$PWD\.nis-mcp-env\Scripts\nis-mcp.exe"
  ```
- With [uv](https://docs.astral.sh/uv/), one line installs and registers it:
  ```powershell
  claude mcp add nis --scope local -- uvx --from "git+https://github.com/BioNanomics/microscope-agent#subdirectory=packages/nis-elements-mcp" nis-mcp
  ```

## Set up the bridge in NIS

1. Switch on the microscope stand, then start NIS-Elements.
2. Find the file to paste:
   ```powershell
   .nis-mcp-env\Scripts\nis-mcp-bridge job-file
   ```
3. In NIS, open **JOBS**, create a job with one **Python** task, replace the
   task's code with the whole of that file, and save the job (for example as
   `NIS bridge`).
4. Run the job. Its log shows
   `NIS bridge v1 listening on 127.0.0.1:8766, pid <NIS pid>`. The job window
   closes at once; the bridge keeps serving in the background.
5. Check it: `.nis-mcp-env\Scripts\nis-mcp-bridge status`.

**Stop the bridge only with**
```powershell
.nis-mcp-env\Scripts\nis-mcp-bridge shutdown
```
**Never press the JOBS Abort button**: a Python task cannot be interrupted
and NIS freezes. The job also ends by itself after 4 hours with no request.

After upgrading this package, paste the job file into NIS again.

## Tools

| Tool | What it does | Needs `confirm=True` |
|---|---|---|
| `nis_status` | XY and Z position, objective, turret, ND run state, e-stop | no |
| `nis_list_optical_configs` | names of the optical configurations in NIS | no |
| `nis_move_relative(dx_um, dy_um)` | relative XY move | yes |
| `nis_change_objective(name)` | turn the turret to an allowed objective | yes |
| `nis_capture(filename)` | capture with the optical configuration active in NIS, save as `.nd2` in the capture folder | yes |
| `nis_run_experiment(experiment)` | run an ND experiment saved in NIS's ND Acquisition window | yes |
| `nis_finish_experiment()` | end the running ND experiment after its current loop | no |
| `estop(action)` | `engage` or `status`; release is not possible from the MCP client | no |

The bridge cannot select an optical configuration (doing so froze NIS):
choose it in NIS before `nis_capture`. Activating an optical configuration in
NIS may also turn the turret to that configuration's objective.

## Safety rules

- Everything that moves or acquires needs **`confirm=True`**.
- **E-stop**: a flag file (`~/.nis-mcp/ESTOP`). While it exists, every move,
  objective change, capture and ND run is refused, by the client and again by
  the bridge inside NIS. Ending an ND run is always allowed. A STOP window
  opens when the server starts. From a terminal:
  ```powershell
  .nis-mcp-env\Scripts\nis-mcp-estop engage
  .nis-mcp-env\Scripts\nis-mcp-estop status
  .nis-mcp-env\Scripts\nis-mcp-estop release     # a person does this, never the MCP client
  ```
- XY moves are **relative**, at most **1000 µm per axis** per call.
- **Z is never moved.** There is no Z tool.
- Objective changes only to **4x and 10x** by default: their long working
  distances cannot reach the sample.
- ND runs can be limited to named experiments.
- The client checks the limits before sending; the bridge checks the step
  and objective limits again with its own settings. The stricter one wins.
- Captures go only into the capture folder, by plain file name, and never
  overwrite a file.
- Motion and acquisition answer HTTP POST only, and the bridge listens on
  127.0.0.1 only.
- Laser power and saved ND experiments are never changed.

## Settings

All settings are environment variables, read by the MCP server (pass them
with `claude mcp add -e NAME=value`):

| Variable | Meaning | Default |
|---|---|---|
| `NIS_BRIDGE_HOST` | bridge address | `127.0.0.1` |
| `NIS_BRIDGE_PORT` | bridge port | `8766` |
| `NIS_CAPTURE_DIR` | where `nis_capture` saves | `~/nis-mcp/captures` |
| `NIS_LOG_DIR` | event log folder | `~/nis-mcp/logs` |
| `NIS_ESTOP_FILE` | e-stop flag file | `~/.nis-mcp/ESTOP` |
| `NIS_ALLOWED_OBJECTIVES` | magnifications allowed, comma-separated | `4,10` |
| `NIS_ALLOWED_EXPERIMENTS` | ND experiment names allowed, comma-separated | empty: any saved one |
| `NIS_MAX_STEP_UM` | largest XY step per call, µm | `1000` |
| `NIS_NO_ESTOP_PANEL` | `1`: do not open the STOP window | off |
| `NIS_NO_MONITOR` | `1`: do not open the monitor window | off |

The bridge job inside NIS reads `NIS_BRIDGE_PORT`, `NIS_ESTOP_FILE`,
`NIS_MAX_STEP_UM` and `NIS_ALLOWED_OBJECTIVES` from NIS's own environment,
which is usually empty, so it uses the same defaults. To change them there,
set the variables before starting NIS, or edit the defaults at the top of
the job file before pasting it. `NIS_ESTOP_FILE` must point to the same file
on both sides.

## Watch what it does

`nis-mcp-monitor` shows the stage, objective, ND state and e-stop, above a
live list of every action sent through the bridge. It opens by itself when
the server starts. Each action is also written to
`<NIS_LOG_DIR>/events-<date>.jsonl`.

## Testing without a microscope

`nis_elements_mcp.testing` has a fake NIS and a pytest fixture that runs the
real bridge on it, on a free port:

```python
from nis_elements_mcp.testing import bridge, estop_file  # noqa: F401  (fixtures)

def test_my_script_moves(bridge):
    nis, url, client = bridge
    client.move_relative(100, 0)
    assert nis.x == 200.0
```

To run this package's own tests: `pip install -e ".[test]"`, then `pytest`.

## License

MIT, see [LICENSE](LICENSE).
