# nis-mcp-server

An MCP server that runs **inside NIS-Elements**, as a JOBS Python task, and
serves MCP over HTTP on `http://127.0.0.1:8767/mcp`. Nothing to install on
the PC besides NIS: the server uses only the standard library.

Tools so far: `echo`, `change_objective` (4x and 10x only; refused while
the e-stop file `~/.nis-mcp/ESTOP` exists).

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

## How the installer works

NIS keeps jobs in SQLite: a `jobdef` row per job and a `jobprogram` row per
save, whose task list holds each task's settings as base64 of NIS's binary
record format. `install_job.py` copies an existing one-Python-task job and
swaps in a loader script. The format is undocumented, worked out from NIS
6.20's database on this PC; a NIS update could change it.
