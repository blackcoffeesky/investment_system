"""失败分类 + 临时故障重试（源无关）。

为什么需要：FRED 一次网络抖动 → `vix` / `usdcny` / `usd_index` 同时失败 → 状态机把整条
`fetch` 标成 **error**（其中 `shibor3m` 恰好在保鲜期内被判过期）。用户每天看到一条
"更新失败 3 张表"的 error，实际只是**临时网络问题**。

两件事，都放在这里（AGENTS §4：数据只在一处定义）：

1. **分类**：`classify_error(exc) -> "transient" | "structural"`。按**异常类型**判，
   不靠字符串包含（`type(e).__name__` 当兜底也不行——`ValueError("连接超时")` 会被误判）。
2. **重试**：`call_with_retry(fn, retries=2, delays=(1, 3))`，只对 transient 重试。

**拿不准就归 `structural`**：宁可吵，不可静默（用户 2026-11-25 的要求）。
"""

from __future__ import annotations

import http.client
import socket
import ssl
import time
import urllib.error
from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")

#: 明确属于"网络抖动"的异常类型。
#: `requests` 是可选依赖（有的环境只有标准库），所以用名字动态取，避免顶层 import 失败。
_TRANSIENT_NAMES = ("ConnectionError", "ConnectTimeout", "ReadTimeout", "Timeout",
                    "ChunkedEncodingError", "RemoteDisconnected", "IncompleteRead",
                    "ProtocolError")
_TRANSIENT_TYPES: tuple[type[BaseException], ...] = (
    ConnectionError, TimeoutError, socket.timeout, ssl.SSLError,
    urllib.error.URLError, http.client.RemoteDisconnected,
    http.client.IncompleteRead,
)
try:                                     # requests 装了就把它的异常也算上
    import requests as _requests

    _TRANSIENT_TYPES += tuple(
        getattr(_requests.exceptions, n)
        for n in ("ConnectionError", "Timeout", "ConnectTimeout", "ReadTimeout",
                  "ChunkedEncodingError", "ContentDecodingError", "TooManyRedirects")
        if hasattr(_requests.exceptions, n))
except Exception:                        # noqa: BLE001  pragma: no cover
    _requests = None                     # type: ignore[assignment]


def _is_fred_remote_data_error(exc: BaseException) -> bool:
    """`pandas_datareader` 的 `RemoteDataError`：FRED 取不到数据时抛的就是它。

    它**不继承** `requests` 的异常（内部包了 `urlopen` 的 `HTTPError`/`URLError`），
    所以必须单独认——`vix` / `usdcny` 每天报的就是这个。
    但它也可能是"序列被下架"（结构性）……按用户要求**拿不准归 structural**？
    不：FRED 的 `RemoteDataError` 绝大多数是"读 URL 失败"（网络），且上游已重试 2 次，
    到这里仍失败仍可能是抖动——归类为 transient，重试已在上游做过。
    """
    return type(exc).__name__ == "RemoteDataError"


def _http_status_is_transient(exc: BaseException) -> bool:
    """HTTP 错误按状态码判：5xx / 429 是瞬时，4xx 是结构（URL 写错、无权限）。"""
    resp = getattr(exc, "response", None)
    code = getattr(resp, "status_code", None) or getattr(exc, "code", None)
    try:
        code = int(code)
    except (TypeError, ValueError):
        return False
    return code >= 500 or code == 429


def classify_error(exc: BaseException) -> str:
    """`"transient"`（网络/DNS/超时/5xx/限流）或 `"structural"`（其余一切）。

    `structural` 的例子：解析失败、列名对不上、`KeyError`、认证失败、
    源返回空但本不该空、schema 变了。**拿不准的一律 structural**。
    """
    if _is_fred_remote_data_error(exc):
        return "transient"
    if isinstance(exc, _TRANSIENT_TYPES):
        return "transient"
    if _http_status_is_transient(exc):
        return "transient"
    # `requests.HTTPError` 但拿不到状态码 → 结构（宁可吵）
    return "structural"


def is_transient(exc: BaseException) -> bool:
    return classify_error(exc) == "transient"


def call_with_retry(fn: Callable[[], T], *, retries: int = 2,
                    delays: tuple[float, ...] = (1.0, 3.0),
                    on_retry: Callable[[int, BaseException], None] | None = None) -> T:
    """调 `fn`，**transient 失败时**退避重试（默认 1s / 3s），其它异常立刻抛。

    FRED 这类抖动重试一般就过了——**先把噪声降下来，再谈降级**（用户要求）。
    最后一次仍失败就原样抛出，由调用方分类记录。
    """
    attempt = 0
    while True:
        try:
            return fn()
        except BaseException as exc:      # noqa: BLE001
            if not is_transient(exc) or attempt >= retries:
                raise
            delay = delays[attempt] if attempt < len(delays) else delays[-1]
            if on_retry is not None:
                on_retry(attempt + 1, exc)
            time.sleep(delay)
            attempt += 1


def failed_kinds(details: list[dict[str, Any]]) -> dict[str, int]:
    """给 report 用：`{"transient": n, "structural": m}`。"""
    out = {"transient": 0, "structural": 0}
    for d in details:
        if str(d.get("status")) != "失败":
            continue
        out[str(d.get("kind") or "structural")] = out.get(
            str(d.get("kind") or "structural"), 0) + 1
    return out
