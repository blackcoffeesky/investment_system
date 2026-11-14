"""数据源注册表：**一张表在哪取数、怎么取**。

约定
----
所有取数函数签名统一为:

    def f(start: str, end: str, table: str = "", **_) -> pd.Series | pd.DataFrame

- `start` / `end`：ISO 日期字符串，闭区间；API 不支持区间就在函数内过滤。
- `table`：目标表名（手工表、按表名读文件的函数用得上）。
- 返回值只含数据本身，不做对齐、不做 ffill、不写库——那是 `update.py` 的事。

`SOURCES` 是唯一事实来源：改数据源 = 改这里一行，不动更新逻辑。
`table` 写成 `"a|b|c"` 表示一次取数拆成多张单列表（拆表是 update 的职责）。

分层
----
`base` 契约与共享工具 / `ashare` HTTP 与 ashareapi / `akshare` / `other`（用得少的）。
**派生量不在这里**：`breadth` / `dividend_yield` 归 `fin/derive.py`，取数与派生不混。
"""

from __future__ import annotations

from datahub.fin.sources.base import Source, _with_prefix

from datahub.fin.sources.ashare import (
    ashare_company,
    ashare_financials,
    ashare_shares,
)
from datahub.fin.sources.akshare import (
    cn_cpi,
    cn_exports,
    cn_industrial,
    cn_money,
    cn_pmi,
    cn_ppi,
    cn_retail,
    cn_retail_mom,
    etf_shares,
    fut_daily,
    fut_hold,
    index_sina,
    index_tx,
    margin_total,
    shibor3m,
    stock_market_cap,
)
from datahub.fin.sources.other import (
    cot_gold_net,
    fred,
    manual_table,
    us_cpi_surprise,
    world_tension,
    xaus_gold,
)


__all__ = ["SOURCES", "Source"]


SOURCES: list[Source] = [
    # 共享宏观
    Source("vix", fred, {"series_id": "VIXCLS"}, note="FRED"),
    Source("usdcny", fred, {"series_id": "DEXCHUS"}, note="FRED"),
    Source("usd_index", fred, {"series_id": "DTWEXBGS"}, note="FRED 广义美元指数"),
    Source("fed_rate", fred, {"series_id": "DFF"}, note="FRED 联邦基金有效利率"),
    Source("real_yield_10y", fred, {"series_id": "DFII10"}, note="FRED 10 年 TIPS"),
    Source("geopolitic_risk", fred, {"series_id": "GPRC"}, enabled=False,
           note="FRED 无此序列；旧数据冻结，新数据改用 world_tension"),
    Source("world_tension", world_tension, note="World Tension Meter（2026-03 起）"),
    Source("xau_usd", xaus_gold, note="XAUS Gold API；2021-11 之前保留旧值"),

    # 价格与成交
    Source("csi_all", index_tx, {"symbol": "sh000985"}, note="腾讯指数 OHLCV"),
    # 2026-11-14 下线过的两条注册，**表和源都已删**（见 DESIGN §12）：
    # - `csi_all_amount|csi_all_turnover`：csi_all 已合并 amount/turnover 两列
    # - `breadth_adv|breadth_dec|breadth_limitup`：改由 `derive.breadth()` 现算
    Source("csi300", index_sina, {"symbol": "sh000300"}, note="新浪指数收盘"),
    Source("csi1000", index_sina, {"symbol": "sh000852"}, note="新浪指数收盘"),
    Source("gold_cny", fut_daily, {"symbol": "AU0"}, note="上期所主力连续"),
    Source("gold_open_interest", fut_hold, {"symbol": "AU0"}, note="上期所持仓量"),
    Source("gold_cot_net_long", cot_gold_net, note="CFTC Legacy：非商业净多头/未平仓量（%）"),

    # 利率 / 杠杆
    Source("shibor3m", shibor3m, note="Shibor 3M 定价"),
    Source("margin_balance", margin_total, note="沪深两融合计"),

    # 宏观
    Source("cpi_yoy", cn_cpi, note="统计局月度"),
    Source("ppi_yoy", cn_ppi, note="统计局月度"),
    Source("pmi", cn_pmi, note="统计局月度"),
    Source("m1_yoy|m2_yoy|m1_level|m2_level", cn_money, note="货币供应，一并取 4 张表"),
    Source("retail_sales", cn_retail, note="社零累计同比（旧口径）"),
    Source("retail_sales_mom", cn_retail_mom, note="社零当月同比"),
    Source("industrial_yoy", cn_industrial, enabled=False,
           note="仅财经日历接口，已停更（2025-08）"),
    Source("exports_yoy", cn_exports, enabled=False, note="同上"),
    Source("total_shares|total_mktcap", stock_market_cap, note="交易所月度统计"),
    Source("gold_cpi_surprise", us_cpi_surprise, note="FRED CPIAUCSL → 同比 − AR(1) 预期"),

    # 手工
    Source("equity_fin_monthly", manual_table, kind="manual", note="人行社融表解析后导出"),
    Source("insider_net", manual_table, kind="manual", note="内部人净增持"),

    # 需要多步采集
    Source("etf_shares", etf_shares, {"weeks": 8}, shape="long",
           note="上交所份额；只补最近 8 周"),
]

SOURCES += [
    # 全市场清单：一次调用拿全，每月刷一次足够
    Source("company", ashare_company, shape="static", note="ashareapi /v1/stocks"),
    # 当前股本快照：季度刷一次（历史变动靠事件推）
    Source("shares_history", ashare_shares, shape="long", note="ashareapi /v1/snapshot"),
    # 季频财报：报告期 + 披露日，用于算 PE/PB/ROE
    Source("financials", ashare_financials, shape="long", note="ashareapi /v1/finance"),
]
# 2026-11-14 删掉的源（表也一起删了，见 DESIGN §12）：
# - `dividend_annual`（→ `derive.dividend_yield()`）
# - `dividend`（→ 数据并入 `adj_factor`；要全市场分红就写新源直接进 adj_factor）
