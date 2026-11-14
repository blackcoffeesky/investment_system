"""探测 ashareapi 各端点：哪些免费、返回什么、字段能不能对上项目。

    python -m experiment.api_probe                  # 只测免费端点（无需 key）
    python -m experiment.api_probe --key sk-xxxx    # 带上 key 测付费端点

也可用环境变量 `ASHARE_API_KEY`。

**这是试验代码**：不接进 datahub，不写库，只打印结果。
判断"值不值得买"的时候，看的是这几个问题：

1. 免费的够不够用（涨跌分布 / 行情 / 市场总览）
2. 付费的能不能**批量**拿（单只一次 × 5000 只 = 额度不够）
3. 字段口径对不对得上已有的 `stock_daily`（收盘价、成交额单位）
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://api.ashareapi.com/v1"
OUT = Path(__file__).resolve().parent / "out"
OUT.mkdir(exist_ok=True)

# 端点 → (路径, 查询参数, 说明)
FREE = {
    "changedist": ("/changedist", {}, "市场涨跌分布（今日快照：涨跌家数/涨跌停/成交额）"),
    "quote": ("/quote", {"code": "sh000985"}, "行情（返回近 30 日日线）"),
    "market_overview": ("/market-overview", {"type": "summary"}, "市场总览（13 个维度打分）"),
    "hot": ("/hot", {"type": "stock"}, "热搜股票"),
    "kline": ("/kline", {"code": "sh000985", "period": "day", "count": 5}, "日 K"),
    "search": ("/search", {"q": "茅台"}, "代码检索"),
}

PAID = {
    "index_constituent": ("/index-constituent", {"code": "sh000300"}, "指数成分股"),
    "limit_up": ("/limit-up", {}, "涨停池明细"),
    "suspension": ("/suspension", {}, "停复牌"),
    "fund_flow": ("/fund-flow", {"code": "sh600519"}, "个股资金流"),
    "snapshot": ("/snapshot", {"code": "sh600519"}, "全字段画像（含市值/股本）"),
    "technical": ("/technical", {"code": "sh600519"}, "技术指标"),
    "chip": ("/chip", {"code": "sh600519"}, "筹码分布"),
    "usage": ("/usage", {}, "额度用量"),
}


def call(path: str, params: dict, key: str | None, timeout: int = 20) -> dict:
    qs = urllib.parse.urlencode(params)     # 中文参数必须编码，否则 urllib 直接报错
    url = f"{BASE}{path}" + (f"?{qs}" if qs else "")
    req = urllib.request.Request(url, headers={"User-Agent": "quant-probe"})
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"ok": False, "http": e.code,
                "body": e.read()[:400].decode("utf-8", "ignore")}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _shape(res: dict) -> str:
    if not res.get("ok"):
        return f"失败 {res.get('error') or res.get('body') or res.get('http')}"
    st = res.get("structured")
    if isinstance(st, list):
        return f"ok · {len(st)} 行 · 字段 {list(st[0])[:8] if st else []}"
    data = res.get("data")
    if isinstance(data, list):
        return f"ok · 列表 {len(data)} 条 · 字段 {list(data[0])[:8] if data else []}"
    return f"ok · {str(data)[:80]}"


def probe(group: dict, key: str | None, title: str, gap: float = 0.0) -> dict:
    print(f"\n=== {title} ===")
    out = {}
    for name, (path, params, desc) in group.items():
        if gap:
            time.sleep(gap)          # 匿名限流 5 次/分，必须拉开间隔
        res = call(path, params, key)
        out[name] = {"ok": bool(res.get("ok")), "tier": res.get("tier"),
                     "shape": _shape(res), "desc": desc}
        print(f"  {name:18s} {out[name]['shape']}")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", default=os.environ.get("ASHARE_API_KEY", ""))
    ap.add_argument("--gap", type=float, default=None,
                    help="两次调用间隔秒数；默认匿名 13s、带 key 1s")
    a = ap.parse_args(argv)
    key = a.key or None
    gap = a.gap if a.gap is not None else (1.0 if key else 13.0)

    print("base:", BASE, "| key:", "有" if key else "无（只测免费端点）",
          "| gap:", gap, "s")
    result = {"free": probe(FREE, key, "免费端点", gap),
              "paid": probe(PAID, key, "付费端点（无 key 会返回 402/说明）", gap)}

    p = OUT / "api_probe.json"
    p.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果 -> {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
