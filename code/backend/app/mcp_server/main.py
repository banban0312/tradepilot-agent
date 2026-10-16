"""使用官方 MCP SDK 启动 Streamable HTTP 服务。"""

from app.config import settings
from app.mcp_server.server import mcp_server


def main() -> None:
    mcp_server.run(
        transport="streamable-http",
        host=settings.mcp_host,
        port=settings.mcp_port,
        stateless_http=True,
    )


if __name__ == "__main__":
    main()
