"""取数池：哪些股票需要逐只抓。

为什么需要它
------------
`/v1/finance`、`/v1/dividend`、`/v1/snapshot` 都是**单只一次**调用。
全市场 5000 只 × 每次 = 5000 次，标准版总共 10 万次额度，撑不了几轮。

所以低频数据只抓"关心的池子"：
- 默认 = 沪深300 ∪ 中证500（约 800 只）
- 可以用文件覆盖：`datahub/fin/intake/clean/pool.txt`（一行一个代码，带 sh/sz 前缀）
- 也可以给环境变量 `ASHARE_POOL=sh600519,sz000001`

成分股本身来自 `/v1/index-constituent`，一次调用拿全，缓存 30 天。
"""

from __future__ import annotations

import os
from pathlib import Path

POOL_FILE = Path(__file__).resolve().parent / "intake" / "clean" / "pool.txt"
INDEXES = ("sh000300", "sh000905")


def _from_env() -> list[str]:
    raw = os.environ.get("ASHARE_POOL", "")
    return [x.strip() for x in raw.split(",") if x.strip()]


def _from_file() -> list[str]:
    if not POOL_FILE.exists():
        return []
    out = []
    for line in POOL_FILE.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(s)
    return out


def _from_index() -> list[str]:
    """从指数成分股拼池子（一次调用拿全，失败就空）。"""
    from datahub.fin import ashare

    codes: list[str] = []
    for idx in INDEXES:
        res = ashare.get("/index-constituent", code=idx)
        if not res.get("ok"):
            continue
        for row in ashare.rows(res):
            c = row.get("code")
            if c:
                codes.append(str(c))
    return codes


def codes(max_n: int | None = None) -> list[str]:
    """当前池子（去重保序）。优先级：环境变量 > 文件 > 指数成分。"""
    got = _from_env() or _from_file() or _from_index()
    seen, out = set(), []
    for c in got:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out[:max_n] if max_n else out


def size() -> int:
    return len(codes())
