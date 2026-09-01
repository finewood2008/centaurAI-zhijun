"""Local stdio MCP entry point.

The remote Streamable HTTP service lives in ``mcp_remote_server.py``. Both
transports use the same read-only tools and the same localhost backend.
"""

from mcp_tools import create_mcp_server


mcp = create_mcp_server(profile="full")


if __name__ == "__main__":
    mcp.run("stdio")
