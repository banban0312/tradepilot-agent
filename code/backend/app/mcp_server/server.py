"""MCP 服务只暴露用于进程与协议检查的探针，不伪造业务能力。"""

from mcp.server.mcpserver import MCPServer

mcp_server = MCPServer("TradePilot MCP")


@mcp_server.tool()
def health_check() -> dict[str, str]:
    """报告 MCP 传输服务状态；此工具不查询或修改任何业务数据。"""
    return {"service": "tradepilot-mcp", "status": "ok"}
