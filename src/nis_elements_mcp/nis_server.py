# nis_server.py
# ------------------------------------------------------------
# MCP server for driving the microscope through NIS-Elements (see
# nis_tools.py). Needs the NIS bridge job (nis_bridge_job.py) running
# inside NIS.
#
# Run, either way:
#   nis-mcp                                 (installed console script)
#   python -m nis_elements_mcp.nis_server
#
#   { "mcpServers": { "nis": { "command": "nis-mcp" } } }
# ------------------------------------------------------------

from mcp.server.mcpserver import MCPServer

from nis_elements_mcp import estop, nis_monitor, nis_tools

mcp = MCPServer("ConfocalOrchestrator-NIS")

mcp.add_tool(nis_tools.nis_status)
mcp.add_tool(nis_tools.nis_move_relative)
mcp.add_tool(nis_tools.nis_change_objective)
mcp.add_tool(nis_tools.nis_list_optical_configs)
mcp.add_tool(nis_tools.nis_capture)
mcp.add_tool(nis_tools.nis_run_experiment)
mcp.add_tool(nis_tools.nis_finish_experiment)
mcp.add_tool(nis_tools.estop)


def main() -> None:
    """Console entry point (``nis-mcp``): serve the NIS tools over stdio."""
    estop.launch_panel()
    nis_monitor.launch()            # live view of what this server does
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
