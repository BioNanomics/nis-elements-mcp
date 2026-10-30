"""Drive a Nikon NIS-Elements microscope from an MCP client, through a bridge job running inside NIS."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("nis-elements-mcp")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0.0.0+unknown"
