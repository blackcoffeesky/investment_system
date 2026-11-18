"""datahub → 消费方的耦合层：**唯一允许依赖 datahub 的 shared 子包**。

## 谁来调

**`get_data` 是全系统唯一的取数入口**，而且**只在装配点调**：
`*/cli.py`、`service/**`、`*/web/routes.py`（页面入口，等价于 CLI）。
其余地方（`port.py` / `pipeline.py` / `features.py` / `analysis/*`）**一律不许调**——
它们只接收传进来的 `DataFrame`。这条有静态测试钉住（`tests/cross/test_layering.py`），
不是"写在文档里就算"的约定。

## 为什么

模块的核心逻辑不该知道"数据从哪来"：喂假数据就能单测、换源不用改业务代码。
耦合只允许发生在少数几个显式装配点，才能被数清、被审查。

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

对齐口径（`lag_days` + ffill）走 `shared.utils.time_align`，这里不另写一份。

## 缓存

`get_data` 在**进程内**缓存 `(panel, start, end) → 面板`，返回的是**副本**：

- 命中直接返回（实测 **<1ms**；未命中 4~5s，大头是 `dividend_yield` 扫 `stock_daily`）
- **失效**：`DataPort.run_update()` 结束时（`finally` 里，异常路径也清）自动
  `clear_cache()`；`get_data(..., fresh=True)` 是逃生口
- **边界**（知道比不知道好）：多 worker 时**每个进程各一份**；
  **别的进程写库不会让它失效**——只有本进程的 `run_update` 会清。
  所以看到"库里更新了、服务还读旧值"，第一嫌疑就是这里
- 不做 TTL / mtime 校验（刻意：最简单的策略最不容易错）

## 不要绕过它

不要直接 `port.series_map(...)` 再自己拼面板——那是 `series_map` 时代的做法，
等于把装配做两遍（`AGENTS.md` §4'③ 已写死）。

要新的列 ⇒ 在 `_ASHARE_BASE` / `_GOLD_BASE` / `_SPECS` / `_PANEL_DERIVED` 里**声明**，
不要在消费方外面拼。`series_map` 的 docstring 也标注了"上层不要直接调我"。

## 列名从哪来

- 基础列：`_ASHARE_BASE` / `_ASHARE_MACRO` / `_GOLD_BASE` / `_GOLD_MACRO` 四张声明，
  `_BUILD` 决定每个 `Panel` 拼哪几块
- ETF 分组份额：`etf_group_shares` 派生量 → `etf_sse50/csi300/csi1000`（`raw`，不 ffill）
- 通用业务派生量：`_PANEL_DERIVED` 按**面板**声明（`etf_dispersion` / `size_flow` /
  `national_etf` / `national_etf_event`，causual 另加 `dividend_yield`）
- **列名是语义名**（`close`、`csi300`、`margin_balance`），不带表名前缀——
  消费方不该知道数据来自哪张表
"""

from __future__ import annotations

import sys
from enum import Enum
from typing import Any, Mapping

import pandas as pd

from shared.utils import time_align as T


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
    # 2026-11-15 补：展示层（observations.panel）要成交额/换手，csi_all 本来就有
    "amount": ("csi_all", "amount"),
    "turnover": ("csi_all", "turnover"),
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
    # 地缘风险：2026-11-18 起用 `world_tension`（WTM，0–100）**替换**
    # `geopolitic_risk`（GPRC，50–450）。**这是换测量、不是换数据源**：量级差 4 倍、
    # 重叠 139 天相关 0.41，**没做任何缩放**。WTM 只从 2026-03-23 起有数据 →
    # 面板里那一段 **NaN 是预期**，不要拿 GPRC 去补（会造出两段不同尺子的拼接序列）。
    "world_tension": "world_tension",
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
    # 地缘风险：2026-11-18 起用 `world_tension`（WTM，0–100）**替换**
    # `geopolitic_risk`（GPRC，50–450）。**这是换测量、不是换数据源**：量级差 4 倍、
    # 重叠 139 天相关 0.41，**没做任何缩放**。WTM 只从 2026-03-23 起有数据 →
    # 面板里那一段 **NaN 是预期**，不要拿 GPRC 去补（会造出两段不同尺子的拼接序列）。
    "world_tension": "world_tension",
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

# 宽基 ETF 分组份额：**分组知识与求和都在数据层**
# （`datahub.fin.derive.DERIVED["etf_group_shares"]`）。这里只留列名，
# 不再有 ETF 代码 / `bare_code` / 组内求和逻辑——2026-11-14 的事故（两侧归一规则
# 不一致、四个派生列整体消失）根因就是这种知识散在消费方。
_ETF_GROUP_KEYS: tuple[str, ...] = ("sse50", "csi300", "csi1000")
_ETF_COLS: tuple[str, ...] = tuple(f"etf_{g}" for g in _ETF_GROUP_KEYS)
#: 请求 `series_map` 时用的派生量名字（见 `fin/derive.DERIVED`）
_ETF_DERIVED = "etf_group_shares"

# 通用业务派生量 → 面板列：(派生量名, 派生量里的列名, 对齐策略)。
# 这些量**自己读库**（`datahub.fin.derive`），adapter 只按名字取，不再吃面板列。
_ASHARE_DERIVED: tuple[tuple[str, str, str], ...] = (
    ("etf_dispersion", "dispersion", "daily"),
    ("size_flow", "flow", "daily"),
    ("national_etf", "net", "daily"),
    ("national_etf_event", "event", "daily"),
)
#: 年度股息率：`year Y` 的值到 `Y+1-01-01` 才可见（防前视），再 ffill 到日频。
#: **只给 causual.ashare**——算一次要 3.7 秒（扫一遍 `stock_daily`），状态层用不上。
_DIVIDEND_YIELD: tuple[tuple[str, str, str], ...] = (
    ("dividend_yield", "yield_mcap_weighted", "daily"),
)
_GOLD_DERIVED: tuple[tuple[str, str, str], ...] = (
    ("cb_gold_buy", "flow", "raw"),
    ("etf_flow", "flow", "raw"),
)
#: 每个面板挂哪些派生列（**按 Panel 而不是 builder**：`ashare_base` 同时被 state 和
#: causual 用，而 `dividend_yield` 一次要 3.7 秒、state 又用不上——不能因为它俩共享
#: builder 就让 state 也付这个代价）。
_PANEL_DERIVED: dict[Panel, tuple[tuple[str, str, str], ...]] = {
    Panel.STATE_ASHARE: _ASHARE_DERIVED,
    Panel.CAUSUAL_ASHARE: _ASHARE_DERIVED + _DIVIDEND_YIELD,
    Panel.STATE_GOLD: (),
    Panel.CAUSUAL_GOLD: _GOLD_DERIVED,
}

# ---------------------------------------------------------------- 每列策略
# `daily`：下游要"每天能看到的最新值"（做逐日 diff / z-score / 展示）→ ffill 到交易日
# `raw`  ：下游要做 diff 或**按观测频率聚合** → 只在观测日有值，不铺满
# 判据：这列的值本身是"某一天的观测"还是"每天都在变的状态"。
_DAILY, _RAW = "daily", "raw"

# 注意：**adapter 是唯一的声明处**，所以这里必须给每一列一个结论。
# 写法统一为"先全量给默认，再显式覆盖存量列"——漏一列会在下面的一致性断言里报错。
# （`build()` 的默认 `raw` 是给其他调用方的安全网：忘声明 = 没对齐，不是悄悄 ffill。）

_ASHARE_BASE_POLICY: dict[str, str] = {
    **{c: _DAILY for c in _ASHARE_BASE},
    # 份额存量：下游按观测日 pct_change，ffill 会把变化抹成 0
    **{c: _RAW for c in _ETF_COLS},
}

_ASHARE_MACRO_POLICY: dict[str, str] = {
    **{c: _DAILY for c in _ASHARE_MACRO},
    # 水平/存量：下游先 `resample("ME").last()` 再聚合，**不能**先 ffill
    "m1_level": _RAW,
    "m2_level": _RAW,
    "total_mktcap": _RAW,
    "equity_fin_monthly": _RAW,
    "insider_net": _RAW,
}

_GOLD_BASE_POLICY: dict[str, str] = {c: _DAILY for c in _GOLD_BASE}

_GOLD_MACRO_POLICY: dict[str, str] = {
    **{c: _DAILY for c in _GOLD_MACRO},
    # 持仓存量：按季/月观测，"每天都能看到"是假的；要流入就按观测日差分
    "cb_holdings": _RAW,
    "gold_etf_holdings": _RAW,
}

_POLICY: dict[str, Mapping[str, str]] = {
    "ashare_base": _ASHARE_BASE_POLICY,
    "ashare_macro": _ASHARE_MACRO_POLICY,
    "gold_base": _GOLD_BASE_POLICY,
    "gold_macro": _GOLD_MACRO_POLICY,
}

# ---------------------------------------------------------------- 每列 warmup
# 取数时**往前多取**多少日历天：`get_data(start=...)` 的取数起点 = start - max(warmup)。
# 为什么需要：低频列在窗口起点之前必须有一个观测，`ffill` 才有东西可填；
# 否则窗口头部会是 NaN（实测 CAUSUAL_GOLD 2025-01-01 起取，头部 23 行差异）。
# 0 = 当天就够（价格类，本身就是日频且窗口内每天都有）。
_WARMUP: dict[str, Mapping[str, int]] = {
    "ashare_base": {
        **{c: 0 for c in ("open", "high", "low", "close", "volume", "csi300", "csi1000")},
        **{c: 10 for c in ("shibor3m", "margin_balance")},
        **{c: 10 for c in _ETF_COLS},          # 份额存量：日频，10 天足够跨个长假
    },
    "ashare_macro": {
        **{c: 10 for c in ("vix", "usdcny", "usd_index", "fed_rate",
                           "real_yield_10y", "world_tension")},
        # 月频：45 天 = 1 个月 + 公布滞后余量
        **{c: 45 for c in ("pmi", "cpi_yoy", "ppi_yoy", "retail_sales",
                           "retail_sales_mom", "industrial_yoy", "exports_yoy",
                           "m1_yoy", "m2_yoy")},
        **{c: 45 for c in ("m1_level", "m2_level", "total_mktcap",
                           "equity_fin_monthly", "insider_net")},
    },
    "gold_base": {
        **{c: 0 for c in ("open", "high", "low", "close", "volume")},
        "usdcny": 10, "vix": 10,
    },
    "gold_macro": {
        **{c: 10 for c in ("usd_index", "fed_rate", "real_yield_10y",
                           "world_tension", "xau_usd", "gold_open_interest",
                           "gold_cot_net_long", "gold_cpi_surprise")},
        "gold_etf_holdings": 45,               # 月频 + 40 天公布滞后
        "cb_holdings": 130,                    # 季频 + 45 天公布滞后
    },
}

# 一致性：每个 spec 列都必须有策略（少一列就报，别等到运行时 KeyError）
for _name, _spec in _SPECS.items():
    _missing = set(_spec) - set(_POLICY[_name])
    if _missing:
        raise RuntimeError(f"{_name} 的列没有声明策略：{sorted(_missing)}")

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


def _build_ashare_base(series: Mapping[str, pd.Series],
                       meta: pd.DataFrame) -> pd.DataFrame:
    """A 股基础面板：价格/指数/两融走 `build`，ETF 分组份额取自**派生量**。

    `etf_group_shares` 是 `series_map` 按名字现算的（分组与求和都在数据层），
    这里只把 `etf_group_shares:sse50` 之类的 key 落到 `etf_sse50` 列上，
    并按原生观测日 reindex（不 ffill）。
    """
    df = T.build(_ASHARE_BASE, "csi_all", series, meta,
                 policy=_ASHARE_BASE_POLICY)
    idx = df.index
    for g in _ETF_GROUP_KEYS:
        s = series.get(f"{_ETF_DERIVED}:{g}")
        if s is None:
            _warn(f"series 里没有 {_ETF_DERIVED}:{g}，etf_{g} 整列 NaN")
            df[f"etf_{g}"] = pd.Series(index=idx, dtype=float)
        else:
            df[f"etf_{g}"] = s.reindex(idx)
    return df


def _plain(builder: str):
    """普通 builder：一张主轴表 + 一份列映射 + 一份策略。"""
    def build(series: Mapping[str, pd.Series], meta: pd.DataFrame) -> pd.DataFrame:
        return T.build(_SPECS[builder], _INDEX[builder], series, meta,
                       policy=_POLICY[builder])
    return build


_BUILDERS = {
    "ashare_base": _build_ashare_base,
    "ashare_macro": _plain("ashare_macro"),
    "gold_base": _plain("gold_base"),
    "gold_macro": _plain("gold_macro"),
}


def columns_of(panel: Panel) -> dict[str, str]:
    """该 Panel 声明的列 → 策略（顺序 = 先 base 后 macro）。"""
    out: dict[str, str] = {}
    for name in _BUILD[panel]:
        for col in _SPECS[name]:
            out[col] = _POLICY[name][col]
        for col in _EXTRA_COLS.get(name, ()):    # ETF 分组份额：raw
            out[col] = _RAW
    # 派生列统一追加在**最后**——`get_data` 是 concat 完之后才挂它们，
    # 顺序必须与这里一致（契约测试按列顺序断言）
    for dname, _, how in _PANEL_DERIVED[panel]:
        out[dname] = how
    return out


def tables_of(panel: Panel) -> tuple[str, ...]:
    """这个面板底层读了哪些表（给 module 的 `REQUIRED_TABLES` 做等式校验用）。

    和 `columns_of` 是兄弟：一个说"读了哪些表"，一个说"给了哪些列"。
    资产包把 `REQUIRED_TABLES` 搬进 `config.py` 后，用
    `set(config.REQUIRED_TABLES) == set(tables_of(Panel.X))` 钉死，防止声明与实现
    漂成两份真相。
    """
    names = _BUILD[panel]
    tables = {_table_of(spec) for name in names for spec in _SPECS[name].values()}
    tables |= {t for name in names for t in _EXTRA_TABLES.get(name, ())}
    return tuple(sorted(tables))


#: 契约：`get_data(p)` 的列**顺序与集合**必须等于 `COLUMNS[p]` 的键，
#: 且每列的对齐语义由值（`daily` / `raw`）声明。
COLUMNS: dict[Panel, dict[str, str]] = {p: columns_of(p) for p in _BUILD}


#: 进程内缓存：`(panel, start, end) → 对齐好的面板`。
#: 为什么值得：service 一次运行会取 4 次 panel（日频/月频 × 2 资产），其中
#: `dividend_yield` 一次 4 秒——缓存能砍掉一半。失效策略是**最朴素的**：
#: `run_update()` 结束时 `clear_cache()`（数据变了就丢），外加 `fresh=True` 强制绕过。
#: 不做 TTL / mtime 校验（后话）。
_CACHE: dict[tuple[Panel, str | None, str | None], pd.DataFrame] = {}


def freshness(panel: Panel, *, today: str | None = None) -> list[dict]:
    """该面板涉及的表的新鲜度（每行带 `freq` / `stale` / `overdue` / `text`）。

    装配点（`cli.py` / `service/`）用它，**消费方靠参数接收结果**——
    和 `get_data` 一样是"只在装配点调"的接口，核心代码不要自己调。

    用 `tables_of(panel)`：`static` / `eav` 表会自动跳过（没有日期轴）。
    `freq` 一定在结果里——下游（月频卡的归档月）靠它筛 `freq in ("M","Q")`，
    不需要再维护一份手写表名清单（那份清单已经漂过两次）。
    """
    from datahub.port import get_port

    return get_port().freshness(tables_of(panel), today=today)


def clear_cache() -> None:
    """清空面板缓存。**数据写入后必须调**（`DataPort.run_update` 已自动调）。"""
    _CACHE.clear()


def get_data(panel: Panel, *, start: str | None = None,
             end: str | None = None, fresh: bool = False) -> pd.DataFrame:
    """取一个面板：`index = DatetimeIndex`（升序、无重复），列见 `COLUMNS[panel]`。

    - 低频列按 `metadata.lag_days` 推后、再 ffill 到交易日（口径在
      `shared.utils.panel_align`，这里不重写）
    - `etf_sse50/etf_csi300/etf_csi1000` 是**分组份额**，按原生观测日给（不 ffill）
    - `start` / `end` 是**闭区间**，按行切片
    - 取不到就抛（`KeyError` 带列名/表名）——消费方不该拿到一张悄悄缺列的面板

    ⚠️ 股息率（`dividend_yield`）暂时还在装配点塞给 `panel()`（`series`），
    第二批才做成通用聚合。ETF 派生列需要的分组份额已经在返回里了。
    """
    key = (panel, start, end)
    if not fresh and key in _CACHE:
        return _CACHE[key].copy()      # 返回副本：调用方改它不该污染缓存

    builders = _BUILD.get(panel)
    if builders is None:
        raise KeyError(f"未知 panel：{panel!r}；可用：{[p.name for p in _BUILD]}")

    from datahub.port import get_port

    port = get_port()
    meta = port.metadata()
    tables = sorted({_table_of(spec)
                     for name in builders for spec in _SPECS[name].values()})
    # ETF 分组份额是**派生量**（`fin/derive.DERIVED["etf_group_shares"]`）：
    # 分组与求和都在数据层，这里只按名字取，**不直接读 etf_shares**。
    names = list(tables)
    if any(_EXTRA_COLS.get(name) for name in builders):
        names.append(_ETF_DERIVED)
    derived_cols = list(_PANEL_DERIVED[panel])
    names += [d[0] for d in derived_cols]
    # 窗口**下推到 SQL**（`stock_daily` 上千万行，不推下去就是把整表读进内存再丢）。
    # 取数起点 = start - max(warmup)：低频列必须在窗口起点之前有一个观测，
    # ffill 才有东西可填（否则窗口头部是 NaN，见 `_WARMUP` 的注释）。
    warm = max((_WARMUP[name].get(col, 0)
                for name in builders for col in _SPECS[name]), default=0)
    fetch_start = None
    if start is not None and warm:
        fetch_start = (pd.Timestamp(start) - pd.Timedelta(days=warm)).strftime("%Y-%m-%d")
    series = port.series_map(names, start=fetch_start or start, end=end)

    frames = [_BUILDERS[name](series, meta) for name in builders]
    df = pd.concat(frames, axis=1).sort_index()
    for dname, src_col, how in derived_cols:
        s = series.get(f"{dname}:{src_col}")
        if s is None:
            _warn(f"series 里没有 {dname}:{src_col}，{dname} 整列 NaN")
            df[dname] = pd.Series(index=df.index, dtype=float)
        else:
            # 年度口径（`index` 是年份整数，不是日期）：年 Y 的值到 **Y+1-01-01** 才可见
            # （年 Y 的除息日都落在 Y 内，数据到 Y+1 才完整）——这是"数据期→可见日"的
            # 换算，和 `lag_days` 同类，所以放在适配层而不是消费方。
            if not isinstance(s.index, pd.DatetimeIndex):
                s = pd.Series(
                    s.to_numpy(dtype=float),
                    index=pd.DatetimeIndex([pd.Timestamp(year=int(y) + 1, month=1, day=1)
                                            for y in s.index]))
            df[dname] = T.to_daily(s, df.index, 0, ffill=(how == "daily"))
    df = df.loc[:, ~df.columns.duplicated()]

    if start is not None:
        df = df[df.index >= pd.Timestamp(start)]
    if end is not None:
        df = df[df.index <= pd.Timestamp(end)]
    _CACHE[key] = df
    return df.copy()
