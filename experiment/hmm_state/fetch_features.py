"""抓取中金所股指期货基差与持仓，输出 data/股指期货基差与持仓.csv
python -m experiment.hmm_state.fetch_futures 2015-01-01 2026-11-01
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
OUT_CSV = ROOT / "data" / "股指期货基差与持仓.csv"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"}

SPOT_LOCAL = {"IF": "csi300", "IM": "csi1000"}
SPOT_TX = {"IC": "sh000905", "IH": "sh000016"}
COL_MAP = {
    "合约代码": "symbol", "今开盘": "open", "最高价": "high", "最低价": "low",
    "持仓量": "open_interest", "持仓变化": "oi_chg", "今结算": "settle"
}


def fetch_month(ym: str, retries=3) -> pd.DataFrame:
    url = f"http://www.cffex.com.cn/sj/historysj/{ym}/zip/{ym}.zip"
    for i in range(retries):
        try:
            resp = requests.get(url, headers=UA, timeout=90)
            if resp.status_code != 200 or resp.content[:2] != b"PK":
                raise RuntimeError("not zip")
            frames = []
            with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                for name in zf.namelist():
                    if not name.lower().endswith(".csv"):
                        continue
                    txt = zf.read(name).decode("gb2312", errors="ignore")
                    df = pd.read_csv(io.StringIO(txt))
                    df["date"] = pd.to_datetime(name[:8], format="%Y%m%d")
                    frames.append(df)
            df = pd.concat(frames).rename(columns=COL_MAP)
            num_cols = ["open", "high", "low", "settle", "open_interest", "oi_chg"]
            for c in num_cols:
                if c in df.columns:
                    df[c] = pd.to_numeric(df[c], errors="coerce")
            return df
        except Exception:
            if i == retries -1:
                return pd.DataFrame()
            time.sleep(2*(i+1))
    return pd.DataFrame()


def spot_series():
    from datahub.port import get_port
    out = {}
    port = get_port()
    for variety, tbl in SPOT_LOCAL.items():
        try:
            s = port.read(tbl, column="close")
            if isinstance(s, pd.DataFrame):
                s = s.iloc[:,0]
            out[variety] = s.astype(float)
            print(f"现货 {variety} 读取本地 {tbl}")
        except Exception as e:
            print(f"{variety}本地现货读取失败 {e}")

    # fallback akshare
    import os
    for k in ("HTTP_PROXY","HTTPS_PROXY","http_proxy","https_proxy"):
        os.environ.pop(k, None)
    import akshare as ak
    for var, code in SPOT_TX.items():
        try:
            df = ak.stock_zh_index_daily_tx(symbol=code)
            df["date"] = pd.to_datetime(df["date"])
            out[var] = df.set_index("date")["close"]
            print(f"现货 {var} akshare {code}")
        except Exception as e:
            print(f"{var} akshare失败 {e}")
    return out


def third_friday(ym:str):
    first = pd.Timestamp(f"20{ym[:2]}-{ym[2:]}-01")
    first_fri = first + pd.Timedelta(days=(4-first.weekday())%7)
    return first_fri + pd.Timedelta(days=14)


def build(start:str, end:str):
    months = pd.period_range(start, end, freq="M").strftime("%Y%m").tolist()
    print(f"下载 {len(months)} 个月中金所数据")
    frames = []
    for m in months:
        d = fetch_month(m)
        if not d.empty:
            frames.append(d)
    raw = pd.concat(frames, ignore_index=True)
    raw["symbol"] = raw["symbol"].astype(str).str.strip()
    raw = raw[raw["symbol"].str.match(r"^(IF|IH|IC|IM)\d{4}$")]
    raw["variety"] = raw["symbol"].str[:2]
    raw["contract_month"] = raw["symbol"].str[2:]
    raw["expiry"] = raw["contract_month"].map(third_friday)
    raw["days_to_expiry"] = (raw["expiry"] - raw["date"]).dt.days
    spots = spot_series()
    keep = raw[raw["variety"].isin(spots)].copy()
    keep["spot_close"] = [spots[v].get(d, float("nan")) for v,d in zip(keep["variety"], keep["date"])]
    keep = keep.dropna(subset=["spot_close"])
    keep["basis"] = keep["settle"] - keep["spot_close"]
    keep["basis_rate"] = keep["basis"] / keep["spot_close"]
    keep["annual_basis_rate"] = keep["basis_rate"] * 365 / keep["days_to_expiry"].where(keep["days_to_expiry"]>0)
    keep.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    print(f"输出 {OUT_CSV}, {len(keep)}行")
    return keep


def main():
    args = sys.argv[1:]
    start = args[0] if args else "2015-01-01"
    end = args[1] if len(args)>=2 else pd.Timestamp.today().strftime("%Y-%m-%d")
    build(start, end)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
