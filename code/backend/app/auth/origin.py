"""拒绝跨源的状态变更请求；部署反向代理时配置外部同源地址。"""

from urllib.parse import urlsplit

from fastapi import Request

from app.config import settings


def _origin_parts(value: str) -> tuple[str, str] | None:
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return None
        if parsed.username or parsed.password or parsed.path not in {"", "/"}:
            return None
        if parsed.query or parsed.fragment:
            return None
        return parsed.scheme.casefold(), parsed.netloc.casefold()
    except ValueError:
        return None


def is_same_origin(request: Request) -> bool:
    supplied = _origin_parts(request.headers.get("origin", ""))
    expected_value = settings.app_origin or str(request.base_url)
    expected = _origin_parts(expected_value)
    return supplied is not None and expected is not None and supplied == expected
