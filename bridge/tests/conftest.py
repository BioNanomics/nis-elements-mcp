# Tests never write logs or captures into the user's ~/nis-mcp: point both
# folders at a throwaway directory before any test runs.
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="nis-mcp-tests-")
os.environ["NIS_LOG_DIR"] = os.path.join(_TMP, "logs")
os.environ["NIS_CAPTURE_DIR"] = os.path.join(_TMP, "captures")
