"""抓取中金所股指期货「基差 + 持仓」，落到 `data/股指期货基差与持仓.csv`。

    python -m experiment.hmm_state.fetch_futures [start] [end]

## 和 `data/股指期货基差与持仓_抓取.py` 的关系

那份脚本能跑，但**现货指数走 akshare 新浪源**，这台机器上时好时坏。
这里只改一处：**现货用本地库**（`csi300` / `csi1000`），拿不到对应指数就跳过该品种，
并在报告里写明跳过哪些——不静默补一个假数。

## 口径

```
基差      = 结算价 − 现货收盘
基差率    = 基差 / 现货
年化基差率 = 基差率 × 365 / 剩余天数      （跨月可比）
near_contract = 每个 (日期, 品种) 里到期日最近的那个合约
```

**当月合约临近交割时，分母（剩余天数）接近 0，年化基差率会被放大**，
所以做信号时默认用次月合约，或对年化值做截尾。
"""

from __future__ import annotations

import io
import sys
import time
import zipfile
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "股指期货基差与持仓.csv"

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"}
#: 品种 → 本地库里的现货表（缺的走 akshare 腾讯源，见 `spot_series`）
SPOT_LOCAL = {"IF": "csi300", "IM": "csi1000", "IC": None, "IH": None}
#: 本地库没有的品种 → akshare 腾讯源代码
SPOT_TX = {"IC": "sh000905", "IH": "sh000016", "IF": "sh000300", "IM": "sh000852"}
COL_MAP = {"合约代码": "symbol", "今开盘": "open", "最高价": "high", "最低价": "low",
           "成交量": "volume", "成交金额": "turnover", "持仓量": "open_interest",
           "持仓变化": "oi_chg", "今收盘": "close", "今结算": "settle",
           "前结算": "pre_settle"}


def fetch_month(ym: str, retries: int = 3) -> pd.DataFrame:
    url = f"http://www.cffex.com.cn/sj/historysj/{ym}/zip/{ym}.zip"
    for i in range(retries):
        try:
            r = requests.get(url, headers=UA, timeout=90)
            if r.status_code != 200 or r.content[:2] != b"PK":
                raise RuntimeError(f"HTTP {r.status_code}")
            frames = []
            with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                for name in z.namelist():
                    if not name.lower().endswith(".csv"):
                        continue
                    txt = z.read(name).decode("gb2312", errors="ignore")
                    df = pd.read_csv(io.StringIO(txt))
                    df["date"] = pd.to_datetime(name[:8], format="%Y%m%d", errors="coerce")
                    frames.append(df)
            out = pd.concat(frames, ignore_index=True).rename(columns=COL_MAP)
            for c in ["open", "high", "low", "close", "settle", "pre_settle", "volume",
                      "turnover", "open_interest", "oi_chg"]:
                if c in out.columns:
                    out[c] = pd.to_numeric(out[c], errors="coerce")
            return out
        except Exception as e:  # noqa: BLE001
            if i == retries - 1:
                print(f"  {ym} 放弃：{type(e).__name__} {e}")
                return pd.DataFrame()
            time.sleep(2 * (i + 1))
    return pd.DataFrame()


def _tx_spot(code: str) -> pd.Series:
    """akshare 腾讯源日线。**必须先清代理**：规则模式会把东方财富/新浪转到国外节点。"""
    import os

    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
              "ALL_PROXY", "all_proxy"):
        os.environ.pop(k, None)
    import akshare as ak

    df = ak.stock_zh_index_daily_tx(symbol=code)
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date")["close"].astype(float).sort_index()


def spot_series() -> dict[str, pd.Series]:
    """现货收盘：本地库优先，缺的走 akshare 腾讯源并缓存到 `data/spot_index.csv`。"""
    from datahub.port import get_port

    port = get_port()
    out: dict[str, pd.Series] = {}
    for variety, table in SPOT_LOCAL.items():
        if not table:
            continue
        try:
            s = port.read(table, column="close")
            if isinstance(s, pd.DataFrame):
                s = s.iloc[:, 0]
            out[variety] = s.astype(float).rename("spot_close")
            print(f"  现货 {variety} ← 本地 {table}（{len(s)} 行）")
        except Exception as e:  # noqa: BLE001
            print(f"  现货 {variety} 本地不可用：{type(e).__name__}")

    cache = ROOT / "data" / "spot_index.csv"
    for variety, code in SPOT_TX.items():
        if variety in out or code not in ("sh000905", "sh000016"):
            continue
        try:
            s = _tx_spot(code)
            out[variety] = s.rename("spot_close")
            print(f"  现货 {variety} ← akshare 腾讯 {code}（{len(s)} 行）")
            s.rename("close").to_csv(cache, encoding="utf-8-sig")
        except Exception as e:  # noqa: BLE001
            print(f"  现货 {variety} 腾讯源失败：{type(e).__name__} {str(e)[:60]}")
    return out


def third_friday(ym: str) -> pd.Timestamp:
    first = pd.Timestamp(f"20{ym[:2]}-{ym[2:]}-01")
    first_fri = first + pd.Timedelta(days=(4 - first.weekday()) % 7)
    return first_fri + pd.Timedelta(days=14)


def build(start: str, end: str) -> pd.DataFrame:
    months = [x.strftime("%Y%m") for x in pd.period_range(pd.Timestamp(start),
                                                          pd.Timestamp(end), freq="M")]
    print(f"下载 {len(months)} 个月的中金所数据包…")
    frames = []
    for i, m in enumerate(months, 1):
        df = fetch_month(m)
        if not df.empty:
            frames.append(df)
        if i % 12 == 0:
            print(f"  已处理 {i}/{len(months)} 月，累计 {sum(len(f) for f in frames)} 行")
    raw = pd.concat(frames, ignore_index=True)
    # ⚠️ 中金所 CSV 的合约代码带**尾随空格**（"IC1607        "），
    # 不 strip 的话正则一条都匹配不上：2026-11-21 踩过，结果只剩最近两个月有数据。
    raw["symbol"] = raw["symbol"].astype(str).str.strip()
    raw = raw[raw["symbol"].str.match(r"^(IF|IH|IC|IM)\d{4}$")].copy()
    raw["variety"] = raw["symbol"].str[:2]
    raw["contract_month"] = raw["symbol"].str[2:]
    raw["expiry"] = raw["contract_month"].map(third_friday)
    raw["days_to_expiry"] = (raw["expiry"] - raw["date"]).dt.days

    spots = spot_series()
    keep = raw[raw["variety"].isin(spots)].copy()
    keep["spot_close"] = [spots[v].get(d, float("nan")) if d in spots[v].index else float("nan")
                          for v, d in zip(keep["variety"], keep["date"])]
    keep = keep.dropna(subset=["spot_close"])

    keep["basis"] = keep["settle"] - keep["spot_close"]
    keep["basis_rate"] = keep["basis"] / keep["spot_close"]
    keep["annual_basis_rate"] = (keep["basis_rate"] * 365 /
                                 keep["days_to_expiry"].where(keep["days_to_expiry"] > 0))
    keep["near_contract"] = (keep.groupby(["date", "variety"])["contract_month"]
                             .transform("min") == keep["contract_month"])

    cols = ["date", "variety", "symbol", "near_contract", "close", "settle", "spot_close",
            "basis", "basis_rate", "annual_basis_rate", "open_interest", "oi_chg",
            "volume", "days_to_expiry"]
    keep = keep[cols].sort_values(["date", "variety", "symbol"])
    keep.to_csv(OUT, index=False, encoding="utf-8-sig")
    return keep


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    start = args[0] if len(args) > 0 else "2015-01-01"
    end = args[1] if len(args) > 1 else pd.Timestamp.today().strftime("%Y-%m-%d")
    df = build(start, end)
    print(f"\n落地 {len(df)} 行 → {OUT}")
    print(f"日期 {df['date'].min().date()} → {df['date'].max().date()}")
    print(df.groupby("variety").agg(起始=("date", "min"), 截止=("date", "max"),
                                    行数=("date", "size")).to_string())
    missing = [v for v, t in SPOT_LOCAL.items() if t is None]
    if missing:
        print(f"\n⚠️ 跳过品种 {missing}：本地库没有对应现货指数（上证50 / 中证500）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
