# settings.py
# ------------------------------------------------------------
# Every setting nis-elements-mcp reads. All come from environment
# variables, so an MCP client config sets them per project, e.g.
#
#   claude mcp add nis -e NIS_CAPTURE_DIR=D:\data\captures -- nis-mcp
#
# Each is read when it is needed, not once at import, except the e-stop
# file (estop.ESTOP_PATH), which every process must agree on from start.
#
#   NIS_BRIDGE_HOST          bridge address              127.0.0.1
#   NIS_BRIDGE_PORT          bridge port                 8766
#   NIS_CAPTURE_DIR          where nis_capture saves     ~/nis-mcp/captures
#   NIS_LOG_DIR              event log folder            ~/nis-mcp/logs
#   NIS_ESTOP_FILE           e-stop flag file            ~/.nis-mcp/ESTOP
#   NIS_ALLOWED_OBJECTIVES   magnifications allowed      4,10
#   NIS_ALLOWED_EXPERIMENTS  ND experiment names allowed (empty = any saved one)
#   NIS_MAX_STEP_UM          largest XY step per call    1000
#   NIS_NO_ESTOP_PANEL       1 = do not open the STOP window
#   NIS_NO_MONITOR           1 = do not open the monitor window
#
# The bridge job inside NIS (nis_bridge_job.py) reads the same names from
# NIS's own environment, with the same defaults; the client and the bridge
# both enforce the limits, so the stricter of the two wins.
# ------------------------------------------------------------

from __future__ import annotations

import os
from pathlib import Path

from nis_elements_mcp import nis_bridge_job as _job


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def bridge_url() -> str:
    host = _env("NIS_BRIDGE_HOST") or _job.DEFAULT_HOST
    port = _env("NIS_BRIDGE_PORT") or _job.DEFAULT_PORT
    return f"http://{host}:{port}"


def capture_dir() -> Path:
    return Path(_env("NIS_CAPTURE_DIR") or Path.home() / "nis-mcp" / "captures")


def log_dir() -> Path:
    return Path(_env("NIS_LOG_DIR") or Path.home() / "nis-mcp" / "logs")


def estop_file() -> Path:
    return _job.estop_file()


def allowed_magnifications() -> tuple[float, ...]:
    return _job.parse_magnifications(_env("NIS_ALLOWED_OBJECTIVES") or _job.DEFAULT_ALLOWED_OBJECTIVES)


def allowed_experiments() -> list[str] | None:
    """The names listed in NIS_ALLOWED_EXPERIMENTS (comma-separated), or None for any."""
    names = [n.strip() for n in _env("NIS_ALLOWED_EXPERIMENTS").split(",") if n.strip()]
    return names or None


def max_step_um() -> float:
    return float(_env("NIS_MAX_STEP_UM") or _job.DEFAULT_MAX_STEP_UM)


def estop_panel_disabled() -> bool:
    return bool(_env("NIS_NO_ESTOP_PANEL"))


def monitor_disabled() -> bool:
    return bool(_env("NIS_NO_MONITOR"))
