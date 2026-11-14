"""ashareapi 的 HTTP 客户端（数据层自己用，不依赖 MCP 插件）。

key 只从环境变量读，绝不写进代码或仓库：

    ASHARE_API_KEY=ct-xxxx

注意 tier：标准版（¥29.9）能用的端点是固定的，`api_tier.py` 里逐个测过。
请求失败不抛异常给上层判断，统一返回 `{"ok": False, "error": ...}`。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

BASE = "https://api.ashareapi.com/v1"
KEY_ENV = "ASHARE_API_KEY"

# 两次请求的最小间隔（标准版 120 次/分，留点余量）
_MIN_GAP = 0.55
_last: list[float] = [0.0]


def key() -> str:
    return os.environ.get(KEY_ENV, "").strip()


def enabled() -> bool:
    return bool(key())


def get(path: str, **params) -> dict[str, Any]:
    """GET 一个端点，返回解析后的 JSON。"""
    if not enabled():
        return {"ok": False, "error": f"没有设置 {KEY_ENV}"}

    gap = time.time() - _last[0]
    if gap < _MIN_GAP:
        time.sleep(_MIN_GAP - gap)
    _last[0] = time.time()

    url = f"{BASE}{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {key()}",
        "User-Agent": "datahub",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        try:
            info = json.loads(body)
        except Exception:  # noqa: BLE001
            info = {"raw": body[:300]}
        info.setdefault("ok", False)
        info["http"] = e.code
        return info
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def rows(res: dict) -> list[dict]:
    """取行数组形态的 data（行数组/表列表/嵌套 data 都兼容）。"""
    data = res.get("data")
    if isinstance(data, dict):          # /stocks 这类：{total, returned, data: [...]}
        inner = data.get("data")
        return [x for x in inner if isinstance(x, dict)] if isinstance(inner, list) else []
    if isinstance(data, list):
        if data and isinstance(data[0], list):     # 表列表
            return data[0]
        return [x for x in data if isinstance(x, dict)]
    st = res.get("structured")
    if isinstance(st, list):
        return st
    return []


def usage() -> dict:
    return get("/usage")
