"""股本变动事件：代码映射 / 规范化规则 / as-of 取股本。

2026-11-28 接入巨潮的全市场股本变动历史（257,357 行）。三条口径各钉一条测试，
因为每一条错了都会**静默错**（列在、数在、结果不对）：
代码不加前缀 → 匹配率 0；不剔"定期报告" → 曲线变平线；不 ×1e4 → 差一万倍。
"""

from __future__ import annotations

import importlib

import pandas as pd

from datahub.fin.sources.share_events import NOT_EVENT, normalize_events, to_prefixed


def test_to_prefixed_by_code_segment():
    """按**交易所代码段**判，不按首字符 —— `920xxx` 属北交所，首字符是 9。"""
    cases = {
        "600519": "sh600519",
        "688981": "sh688981",
        "900901": "sh900901",     # 沪 B
        "000001": "sz000001",
        "002001": "sz002001",
        "300750": "sz300750",
        "301001": "sz301001",
        "200011": "sz200011",     # 深 B
        "920670": "bj920670",
        "830001": "bj830001",
        "1": "sz000001",          # 前导零补齐（000001 是深市）
        "201872": None,           # 深 B 的老代码段，无法判定 → 跳过
        "abcdef": None,
    }
    for raw, want in cases.items():
        assert to_prefixed(raw) == want, (raw, to_prefixed(raw), want)


def _raw() -> pd.DataFrame:
    return pd.DataFrame({
        "证券代码": ["600519", "600519", "000001", "920670"],
        "变动日期": ["2020-06-24", "2021-03-31", "1991-04-03", "2026-11-27"],
        "变动原因": ["转增", "定期报告", "A股上市", "限售股份上市"],
        "变动原因编码": ["015011", "015019", "015001", "015027"],
        "总股本": ["125008.16", "125008.16", "1940591.82", "10618.24"],
        "已流通股份": ["125008.16", "125008.16", "2650.0", "5000.0"],
    })


def test_normalize_drops_periodic_reports_and_converts_units():
    df, st = normalize_events(_raw())
    assert len(df) == 3, df.to_string()                    # 定期报告被剔
    assert st["定期报告（剔除）"] == 1
    # 万股 → 股：125,008.16 万股 = 1,250,081,601 股（与库内快照逐股一致）
    assert abs(df.loc[df["date"] == "2020-06-24", "total_after"].iloc[0]
               - 1_250_081_601) < 2.0   # 浮点：125008.16 万股
    assert set(df["kind"]) == {"event"}
    assert set(df["code"]) == {"sh600519", "sz000001", "bj920670"}


def test_normalize_delta_total_is_nan_for_first_event():
    """第一笔没有"上一笔" → NaN（用 0 硬填会把"上市"记成 +0）。"""
    df, _ = normalize_events(_raw())
    first = df[df["code"] == "sh600519"].iloc[0]
    assert pd.isna(first["delta_total"])


def test_normalize_merges_same_code_same_date():
    """同一天多条（送股 + 转增分两行）→ 合并成一行，原因用 `、` 连。"""
    raw = _raw()
    raw.loc[0, "变动原因"] = "送股"
    raw.loc[0, "变动原因编码"] = "015010"
    raw.loc[1] = ["600519", "2020-06-24", "转增", "015011", "125008.16", "1.0"]
    df, st = normalize_events(raw)
    row = df[(df["code"] == "sh600519") & (df["date"] == "2020-06-24")]
    assert len(row) == 1, row.to_string()
    assert "送股" in row.iloc[0]["event_type"] and "转增" in row.iloc[0]["event_type"]
    assert st["同码同日多条"] == 2


def test_asof_shares_takes_the_point_in_time_value():
    """权重取"该年最后一个交易日**当时**的股本"，不是当前快照。"""
    D = importlib.import_module("datahub.fin.derive")
    px = pd.DataFrame({"code": ["sh600519", "sh600519", "sz000001"],
                       "year": [2020, 2024, 2024],
                       "close": [10.0, 10.0, 5.0],
                       "asof_date": ["2020-12-31", "2024-12-31", "2024-12-31"]})
    sh = pd.DataFrame({"code": ["sh600519", "sh600519", "sz000001"],
                       "date": ["2019-12-31", "2022-06-30", "2010-01-01"],
                       "total_after": [100.0, 200.0, 50.0]})
    out = D._asof_shares(px, sh).set_index(["code", "asof_date"])["shares"]
    assert out.loc[("sh600519", "2020-12-31")] == 100.0     # 2020 年用 2019 那一笔
    assert out.loc[("sh600519", "2024-12-31")] == 200.0     # 2024 年用 2022 那一笔
    assert out.loc[("sz000001", "2024-12-31")] == 50.0      # 只有一笔 → 用它


def test_asof_shares_falls_back_to_first_event():
    """早于第一笔事件的年份 → 用该股**最早**一期股本兜底（不是丢行）。"""
    D = importlib.import_module("datahub.fin.derive")
    px = pd.DataFrame({"code": ["sh600000"], "year": [1998],
                       "close": [1.0], "asof_date": ["1998-12-31"]})
    sh = pd.DataFrame({"code": ["sh600000"], "date": ["2001-01-01"], "total_after": [9.0]})
    out = D._asof_shares(px, sh)
    assert out["shares"].iloc[0] == 9.0


def test_periodic_report_marker_is_single_source():
    """"不是变动"的原因集合只能有一份（数据只在一处定义）。"""
    assert "定期报告" in NOT_EVENT
