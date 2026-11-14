# 已删除的表（2026-11-14）

这 5 张表在 2026-11-14 被删除，这里是**删除前的全量快照**（CSV），留作回溯用。删除原因见 `datahub/DESIGN.md` §12。

| 表 | 行数 | 取代者 |
|---|---|---|
| breadth_adv / breadth_dec / breadth_limitup | 各 1 | `derive.breadth()`（口径更准：分板块阈值、不把创业板 10% 当涨停） |
| dividend_annual | 5 | `derive.dividend_yield()`（旧表 2024/2025 数据本身不完整） |
| dividend | 27 | 已并入 `adj_factor`（`report_end` / `plan` 两列） |

**不要**把这些 CSV 改回表：它们只是历史证据。
