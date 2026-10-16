"""使用官方 MCP SDK 启动 Streamable HTTP 服务。"""

from app.mcp_server.server import run_server


def main() -> None:
    run_server()


if __name__ == "__main__":
    main()
