# Changelog

All notable changes to nis-elements-mcp are recorded here.
Format follows [Keep a Changelog](https://keepachangelog.com/); versions follow
[Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-10-30

First release, extracted from the microscope-agent project.

### Added
- MCP server `nis-mcp` with the tools `nis_status`, `nis_list_optical_configs`,
  `nis_move_relative`, `nis_change_objective`, `nis_capture`,
  `nis_run_experiment`, `nis_finish_experiment` and `estop`.
- Bridge job `nis_bridge_job.py` for a NIS-Elements JOBS Python task (standard
  library only). Capture and ND calls run on NIS's main thread through a
  Win32 timer; called from a worker thread they crashed NIS.
- Commands `nis-mcp-bridge` (by-hand client, `job-file`, `shutdown`),
  `nis-mcp-estop` and `nis-mcp-monitor`.
- E-stop flag file checked by the client and by the bridge, with an
  always-on-top STOP window.
- Event log and live monitor of every action sent through the bridge.
- Settings from `NIS_*` environment variables, including allow-lists for
  objectives and ND experiments and an XY step limit, checked by the client
  and again by the bridge.
- `nis_elements_mcp.testing`: a fake NIS and a pytest fixture for tests
  without a microscope.

### Fixed
- `ND_LoadExperiment`'s return code is reported, not treated as a failure:
  it has no documented value and returned 0 for a template that loads.

### Known limits
- An ND run blocks the bridge until it ends, so `nis_finish_experiment`
  cannot end a run early yet (checked on the microscope 2026-10-30).
- The optical configuration cannot be selected through the bridge.
- The STOP and monitor windows close when the MCP client disconnects.
