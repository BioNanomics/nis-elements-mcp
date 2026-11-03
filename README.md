# nis-mcp-server

An MCP server that runs **inside NIS-Elements**, as a JOBS Python task, and
serves MCP over HTTP on `http://127.0.0.1:8767/mcp`. Nothing to install on
the PC besides NIS: the server uses only the standard library.

## Tools

| Tool | What it does |
|---|---|
| `echo` | returns the text: checks that the server answers |
| `status` | stage XY and Z, objective, nosepiece, ND run state, e-stop state (read-only) |
| `move_relative` | XY move from where the stage is, at most 1000 um per axis; Z never moves |
| `change_objective` | turns the nosepiece and switches NIS's calibration; 4x and 10x only |
| `list_optical_configs` | the optical configurations defined in NIS (read-only; select one in NIS) |
| `capture` | one image with the active optical configuration, saved as ND2 in `~/nis-mcp/captures`, by plain file name, never overwriting |
| `run_experiment` | runs an ND experiment saved in NIS's ND Acquisition window, by name |
| `finish_experiment` | ends the running ND experiment after its current loop |
| `estop` | `engage` creates the e-stop file `~/.nis-mcp/ESTOP`; `status` reports it |

While the e-stop file exists, every move, objective change, capture and ND
run is refused. Only a person releases it, by deleting the file:
```powershell
Remove-Item ~\.nis-mcp\ESTOP
```
`finish_experiment` and `estop` always work: they only stop things.

The limits (step size, objectives, capture folder, `ALLOWED_EXPERIMENTS`)
are constants at the top of `NISMCPServer.py`: edit and save.

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
   the run is over; meanwhile the other tools answer that NIS is busy, so
   `finish_experiment` cannot end it early. Use NIS's **Finish** button in
   the ND Progress window.
2. **NIS does not say whether a saved ND experiment was found**: if not, it
   runs whatever the ND Acquisition window holds. List the allowed names in
   `ALLOWED_EXPERIMENTS`.
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
