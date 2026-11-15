"""datahub → 消费方的耦合层：**唯一允许依赖 datahub 的 shared 子包**。

为什么要有这一层
----------------
2026-11-14 的事故：datahub 把 ETF 代码从裸码改成 `sh510300`，消费方各自按
"代码字符串"做匹配，一个都没匹配上 → 特征列整列 NaN、派生量整体消失，
**全程没有任何报错**。根因是消费方必须知道数据层的内部细节（表名、宽表怎么拆、
代码什么格式）。

这一层把那些细节关在门里：外面只认 `Panel` 枚举 + **语义列名**。
datahub 内部怎么变（表改名、宽表拆开、代码加前缀）都只改这一个文件。

两条硬约束
----------
1. **只依赖 `datahub.port` + `shared.utils`**，不 import causual / state_layer；
2. **`shared/__init__.py` 不许 import 本包**——否则任何 `import shared`
   都会把 datahub + sqlite 拖进来（纯工具那层必须保持无副作用）。

对齐口径（`lag_days` + ffill）走 `shared.utils.panel_align`，这里不另写一份。

列名从哪来
----------
四个 `panel.py` 里的 `COLS` / `ASHARE` / `GOLD` / `SHARED` 映射已经写好了
"要哪张表的哪一列"。本文件把它们按资产 + 层（base / macro）重排成 4 个声明，
`_BUILD` 决定每个 `Panel` 要拼哪几块。**列名是语义名**（`close`、`csi300`、
`margin_balance`），不带表名前缀——消费方不该知道数据来自哪张表。
"""

from __future__ import annotations

import sys
from enum import Enum
from typing import Any, Mapping

import pandas as pd

from shared.utils import panel_align as P


class Panel(Enum):
    """消费方能要的四种面板。值里带命名空间，方便打日志。"""

    CAUSUAL_ASHARE = "causual.ashare"
    CAUSUAL_GOLD = "causual.gold"
    STATE_ASHARE = "state.ashare"
    STATE_GOLD = "state.gold"


# ---------------------------------------------------------------- 声明
# 键 = `get_data` 返回的列名（语义名），值 = `panel_align` 认识的取数 spec：
#   "表名"          单序列表
#   ("表名", "列名") 多列表
# 只放**面板真正要用的列**，不搬整个表——多一列就是多一份要维护的契约。

_ASHARE_BASE: dict[str, Any] = {
    "open": ("csi_all", "open"),
    "high": ("csi_all", "high"),
    "low": ("csi_all", "low"),
    "close": ("csi_all", "close"),
    "volume": ("csi_all", "volume"),
    "csi300": "csi300",
    "csi1000": "csi1000",
    "shibor3m": "shibor3m",
    "margin_balance": "margin_balance",
}

_ASHARE_MACRO: dict[str, Any] = {
    # 共享宏观（黄金也用同一份）
    "vix": "vix",
    "usdcny": "usdcny",
    "usd_index": "usd_index",
    "fed_rate": "fed_rate",
    "real_yield_10y": "real_yield_10y",
    "geopolitic_risk": "geopolitic_risk",
    # A 股宏观 / 供给
    "pmi": "pmi",
    "insider_net": "insider_net",
    "retail_sales": "retail_sales",
    "retail_sales_mom": "retail_sales_mom",
    "cpi_yoy": "cpi_yoy",
    "ppi_yoy": "ppi_yoy",
    "industrial_yoy": "industrial_yoy",
    "exports_yoy": "exports_yoy",
    "m1_yoy": "m1_yoy",
    "m2_yoy": "m2_yoy",
    "m1_level": "m1_level",
    "m2_level": "m2_level",
    "total_shares": "total_shares",
    "total_mktcap": "total_mktcap",
    "equity_fin_monthly": "equity_fin_monthly",
}

# 状态层的黄金面板要 usdcny / vix，所以它们算 base（因果层再叠加 macro）
_GOLD_BASE: dict[str, Any] = {
    "open": ("gold_cny", "open"),
    "high": ("gold_cny", "high"),
    "low": ("gold_cny", "low"),
    "close": ("gold_cny", "close"),
    "volume": ("gold_cny", "volume"),
    "usdcny": "usdcny",
    "vix": "vix",
}

_GOLD_MACRO: dict[str, Any] = {
    "usd_index": "usd_index",
    "fed_rate": "fed_rate",
    "real_yield_10y": "real_yield_10y",
    "geopolitic_risk": "geopolitic_risk",
    "xau_usd": "xau_usd",
    "cb_holdings": "cb_holdings",
    "gold_etf_holdings": "gold_etf_holdings",
    "gold_open_interest": "gold_open_interest",
    "gold_cot_net_long": "gold_cot_net_long",
    "gold_cpi_surprise": "gold_cpi_surprise",
}

_SPECS: dict[str, Mapping[str, Any]] = {
    "ashare_base": _ASHARE_BASE,
    "ashare_macro": _ASHARE_MACRO,
    "gold_base": _GOLD_BASE,
    "gold_macro": _GOLD_MACRO,
}

# 宽基 ETF 分组（裸 6 位码）。**放在这一层**是因为它属于"市场定义"，
# 消费方不该各自维护一份——2026-11-14 的事故就是两侧代码归一规则不一致。
_ETF_GROUPS: dict[str, tuple[str, ...]] = {
    "sse50": ("510050", "510710", "510100", "510190"),
    "csi300": ("510300", "510310", "510330"),
    "csi1000": ("512100", "560010", "510580", "510510"),
}
_ETF_COLS: tuple[str, ...] = tuple(f"etf_{g}" for g in _ETF_GROUPS)

# builder 额外需要的表 / 额外产出的列（不来自 `_SPECS` 的单表/多表映射）
_EXTRA_TABLES: dict[str, tuple[str, ...]] = {"ashare_base": ("etf_shares",)}
_EXTRA_COLS: dict[str, tuple[str, ...]] = {"ashare_base": _ETF_COLS}

# 每个 builder 对齐到哪张表作为交易日主轴
_INDEX = {
    "ashare_base": "csi_all",
    "ashare_macro": "csi_all",
    "gold_base": "gold_cny",
    "gold_macro": "gold_cny",
}

# 声明式组合：一个 Panel = 若干 builder 按顺序叠列
_BUILD: dict[Panel, tuple[str, ...]] = {
    Panel.STATE_ASHARE: ("ashare_base",),
    Panel.STATE_GOLD: ("gold_base",),
    Panel.CAUSUAL_ASHARE: ("ashare_base", "ashare_macro"),
    Panel.CAUSUAL_GOLD: ("gold_base", "gold_macro"),
}


def _warn(msg: str) -> None:
    """降级**要响**：不抛异常（下游还能跑），但必须说出来。"""
    print(f"[adapter] {msg}", file=sys.stderr)


def _table_of(spec: Any) -> str:
    return spec[0] if isinstance(spec, tuple) else str(spec)


def column_of(spec: Any) -> str:
    """面板列在 `get_data` 结果里的名字。

    元组 `(表, 列)` 取**列名**，单表 spec 取**表名**。
    四个 `panel.py` 用它把自己的 `COLS`/`ASHARE` 映射到语义列，不必另维护一份。
    """
    return spec[1] if isinstance(spec, tuple) else str(spec)


def bare_code(name: object) -> str:
    """`sh510300` / `c510300` / `510300.SH` → `510300`。

    数据层 2026-11 起给代码加了市场前缀。归一逻辑**只在这里有一份**，
    消费方要用就 `from shared.adapter import bare_code`（两个 panel 已转出口）。
    """
    digits = "".join(ch for ch in str(name) if ch.isdigit())
    return digits[-6:] if len(digits) >= 6 else str(name)


def _etf_group_shares(series: Mapping[str, pd.Series], meta: pd.DataFrame,
                      idx: pd.DatetimeIndex) -> dict[str, pd.Series]:
    """宽基 ETF **分组份额**（组内求和），按原生观测日给出、**不做 ffill**。

    为什么不能 ffill：派生列（`SIZE_FLOW` 等）是在**观测日**上算 `pct_change`
    再 ffill 结果的。先 ffill 会让非观测日的 pct_change 变成 0，口径就变了。
    所以这里用 `panel_align.to_daily(..., ffill=False)`：只对齐日历、留 NaN，
    消费方 `.dropna()` 就能拿回原生序列。
    """
    keys = [k for k in series if k.startswith("etf_shares:")]
    if not keys:
        _warn("series 里没有 etf_shares:*，ETF 分组份额整块缺席")
        return {c: pd.Series(index=idx, dtype=float) for c in _ETF_COLS}

    wide = pd.DataFrame({bare_code(k.split(":", 1)[1]): series[k] for k in keys})
    lag = P.lag_of("etf_shares", meta)
    out: dict[str, pd.Series] = {}
    for group, codes in _ETF_GROUPS.items():
        cols = [c for c in codes if c in wide.columns]
        name = f"etf_{group}"
        if not cols:
            _warn(f"ETF 组 {group} 一个代码都没匹配上（代码格式又变了？）")
            out[name] = pd.Series(index=idx, dtype=float)
        else:
            out[name] = P.to_daily(wide[cols].sum(axis=1), idx, lag, ffill=False)
    return out


def _build_ashare_base(series: Mapping[str, pd.Series],
                       meta: pd.DataFrame) -> pd.DataFrame:
    df = P.build(_ASHARE_BASE, "csi_all", series, meta)
    for col, s in _etf_group_shares(series, meta, df.index).items():
        df[col] = s
    return df


def _plain(builder: str):
    """普通 builder：一张主轴表 + 一张声明映射。"""
    def build(series: Mapping[str, pd.Series], meta: pd.DataFrame) -> pd.DataFrame:
        return P.build(_SPECS[builder], _INDEX[builder], series, meta)
    return build


_BUILDERS = {
    "ashare_base": _build_ashare_base,
    "ashare_macro": _plain("ashare_macro"),
    "gold_base": _plain("gold_base"),
    "gold_macro": _plain("gold_macro"),
}


def columns_of(panel: Panel) -> tuple[str, ...]:
    """该 Panel 声明的列（顺序 = 先 base 后 macro，组内按声明顺序）。"""
    out: list[str] = []
    for name in _BUILD[panel]:
        out.extend(column_of(spec) for spec in _SPECS[name].values())
        out.extend(_EXTRA_COLS.get(name, ()))
    return tuple(out)


#: 契约：`get_data(p)` 的列集合必须**恰好**等于 `COLUMNS[p]`（顺序也按声明）。
COLUMNS: dict[Panel, tuple[str, ...]] = {p: columns_of(p) for p in _BUILD}


def get_data(panel: Panel, *, start: str | None = None,
             end: str | None = None) -> pd.DataFrame:
    """取一个面板：`index = DatetimeIndex`（升序、无重复），列见 `COLUMNS[panel]`。

    - 低频列按 `metadata.lag_days` 推后、再 ffill 到交易日（口径在
      `shared.utils.panel_align`，这里不重写）
    - `etf_sse50/etf_csi300/etf_csi1000` 是**分组份额**，按原生观测日给（不 ffill）
    - `start` / `end` 是**闭区间**，按行切片
    - 取不到就抛（`KeyError` 带列名/表名）——消费方不该拿到一张悄悄缺列的面板

    ⚠️ 股息率（`dividend_yield`）暂时还在装配点塞给 `panel()`（`series`），
    第二批才做成通用聚合。ETF 派生列需要的分组份额已经在返回里了。
    """
    builders = _BUILD.get(panel)
    if builders is None:
        raise KeyError(f"未知 panel：{panel!r}；可用：{[p.name for p in _BUILD]}")

    from datahub.port import get_port

    port = get_port()
    meta = port.metadata()
    tables = sorted({_table_of(spec)
                     for name in builders for spec in _SPECS[name].values()}
                    | {t for name in builders for t in _EXTRA_TABLES.get(name, ())})
    series = port.series_map(tables)

    frames = [_BUILDERS[name](series, meta) for name in builders]
    df = pd.concat(frames, axis=1).sort_index()
    df = df.loc[:, ~df.columns.duplicated()]

    if start is not None:
        df = df[df.index >= pd.Timestamp(start)]
    if end is not None:
        df = df[df.index <= pd.Timestamp(end)]
    return df
