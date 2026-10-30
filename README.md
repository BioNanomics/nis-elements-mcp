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

Tested on NIS-Elements AR with a Nikon Ti2 stand and an AX confocal: status,
optical configurations, XY moves, objective changes, capture, the e-stop and
ND runs. Ending an ND run early is not yet possible (see Known limits).

## Requirements

- Windows, with NIS-Elements and its **JOBS** module (Python task).
- Python 3.11 or newer on the same PC, for the MCP server.
- Claude Code, if you want to use it from Claude.
- The bridge job needs nothing installed inside NIS: it uses only the Python
  standard library.

## Setup, step by step

Run the commands in **PowerShell**, in your project's folder.

### Step 1. Create a Python environment for the server

```powershell
python -m venv .nis-mcp-env
```

> If this fails with "The filename or extension is too long" (WinError 206),
> your project folder's path is too deep for Windows' 260-character limit.
> Use a shorter folder, or turn on long paths in Windows.

### Step 2. Install the package

```powershell
.nis-mcp-env\Scripts\pip install "git+https://github.com/BioNanomics/microscope-agent#subdirectory=packages/nis-elements-mcp"
```

> From a local copy instead of GitHub:
> `.nis-mcp-env\Scripts\pip install D:\path\to\microscope-agent\packages\nis-elements-mcp`

### Step 3. Register the server with Claude Code

```powershell
claude mcp add nis --scope local -- "$PWD\.nis-mcp-env\Scripts\nis-mcp.exe"
```

> - `$PWD\...` puts in the full path, so the server starts whatever folder
>   Claude is launched from.
> - To choose your own folders or limits, add `-e NAME=value` before the `--`
>   (all names are in [Settings](#settings)):
>   ```powershell
>   claude mcp add nis --scope local -e NIS_CAPTURE_DIR=D:\data\nis\captures -e NIS_LOG_DIR=D:\data\nis\logs -- "$PWD\.nis-mcp-env\Scripts\nis-mcp.exe"
>   ```
> - With [uv](https://docs.astral.sh/uv/), one line replaces Steps 1 to 3:
>   ```powershell
>   claude mcp add nis --scope local -- uvx --from "git+https://github.com/BioNanomics/microscope-agent#subdirectory=packages/nis-elements-mcp" nis-mcp
>   ```

### Step 4. Start the microscope and NIS

Switch on the microscope stand first, then start NIS-Elements.

### Step 5. Put the bridge job into NIS (once, and again after each upgrade)

1. Print the path of the file to paste:
   ```powershell
   .nis-mcp-env\Scripts\nis-mcp-bridge job-file
   ```
2. Open that file in a text editor and copy all of it. Its first line is
   `# IMPORTANT: 'limjob' must be imported like this (not from nor as)`.
3. In NIS, open **JOBS** and create a job with one **Python** task.
4. Replace the task's code with what you copied (Ctrl+A, Ctrl+V).
5. Save the job, for example as `NIS bridge`.

### Step 6. Run the bridge and check it

1. Run the job in NIS. The job window closes at once; the bridge keeps
   running in the background.
2. Check that it answers:
   ```powershell
   .nis-mcp-env\Scripts\nis-mcp-bridge status
   ```
   You should see the stage position and the objectives on the turret.

### Step 7. Use it from Claude

Start a **new** Claude Code session in the project folder (a server added
in Step 3 appears in new sessions only), and ask, for example, "What is the
microscope's status?". A red STOP window and a monitor window open when the
server starts.

### When you are done: stop the bridge

```powershell
.nis-mcp-env\Scripts\nis-mcp-bridge shutdown
```

**Never press the JOBS Abort button**: a Python task cannot be interrupted
and NIS freezes. The bridge also stops by itself after 4 hours with no
request.

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

NIS does not report whether `nis_run_experiment` found the saved name
(`ND_LoadExperiment` has no return value): if the name is wrong, NIS runs
whatever the ND Acquisition window currently holds. Check the name, and set
`NIS_ALLOWED_EXPERIMENTS` to the names you mean to run.

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

## Known limits

1. **An ND run blocks the bridge until it ends.** `nis_run_experiment`
   returns only when the run is over, and nothing else (not even
   `nis_status` or `nis_finish_experiment`) is answered meanwhile. Ending a
   run early from MCP is therefore not possible yet; use NIS's own **Finish**
   button in the ND Progress window.
2. **The optical configuration cannot be selected** through the bridge:
   choose it in NIS before `nis_capture`.
3. **NIS does not report whether a saved ND experiment was found** (see
   Tools); set `NIS_ALLOWED_EXPERIMENTS`.
4. **The STOP and monitor windows close when the MCP client disconnects.**
   The e-stop itself (the flag file) keeps working; open the STOP window by
   hand with `.nis-mcp-env\Scripts\pythonw -m nis_elements_mcp.estop_panel`.

## License

MIT, see [LICENSE](LICENSE).

This project is not affiliated with, endorsed by, or supported by Nikon.
It contains no Nikon code: the bridge calls NIS-Elements' own macro
functions on a PC where NIS-Elements is already installed and licensed.
Nikon and NIS-Elements are trademarks of Nikon Corporation.
