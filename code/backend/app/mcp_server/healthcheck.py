"""以真实 MCP 客户端调用探针，作为 Compose 就绪检查。"""

import asyncio

from mcp import Client

from app.config import settings


async def main() -> None:
    async with Client(settings.mcp_url) as client:
        result = await client.call_tool("health_check", {})
        if result.is_error:
            raise RuntimeError("MCP 健康探针返回协议错误")
        if result.structured_content != {
            "service": "tradepilot-mcp",
            "status": "ok",
        }:
            raise RuntimeError("MCP 健康探针返回内容不符合约定")


if __name__ == "__main__":
    asyncio.run(main())
