# ashareapi 免费层实测（2026-11-14）

用 `python -m experiment.api_probe` 实跑，结果落在 `out/api_probe.json`。

## 免费可用（无需 key）

| 端点 | 返回 | 对项目有什么用 |
|---|---|---|
| `/v1/changedist` | 上涨/下跌/平盘/停牌 + 涨停/跌停家数、两市成交额、11 档涨跌幅分布 | **市场宽度的现成快照**（只有当日，没有历史） |
| `/v1/quote` | 单标的近 30 日日线（open/last/high/low/volume/amount/turnover） | 指数兜底校验；`amount` 是元、`turnover` 是 % |
| `/v1/kline` | 同上，参数化条数 | 与本地 `stock_daily` 对账用 |
| `/v1/market-overview` | 13 个维度的打分 + 文字状态 | 只当"另一个视角"，别当数据用（见下） |
| `/v1/hot` | 热搜股票 | 情绪辅助，先不用 |

## 付费（无 key 一律 `需要 API Key（此端点属付费层）`）

`index-constituent` / `limit-up` / `suspension` / `fund-flow` / `snapshot` / `technical` / `chip` / `usage` / `search`。

## 限流（实测）

- 匿名 **5 次/分**，超了直接报错，且要求解 PoW 才能提到 15 次/分
- 匿名每天另有 10 万条上限
- 所以 `api_probe` 默认两次调用间隔 13 秒；带 key 时 1 秒

## 一个要留意的矛盾

同一天（2026-11-13）两份数据对不上：

- `changedist`：上涨 **765** 家、下跌 **4748** 家（涨股比 13%）
- `market_overview`：`个股宽度 = 5 · 普涨(创新高>200只，或者破位<10只)`

两个都来自免费层。结论：**`market_overview` 的文字判定不能直接采信**，
要用宽度就用 `changedist` 的家数自己算。

## 对"买不买 ¥30"的结论

免费层已经覆盖了"当天宽度"这一块，**历史宽度仍要自己从 `stock_daily` 算**。

付费层值得买的是**算不出来的东西**：

| 端点 | 价值 | 每日调用量 |
|---|---|---|
| `limit-up` | 真实涨停池 + 涨停原因（不用 9.8% 代理） | 1 |
| `suspension` | 停牌原因（比"没数据"多一层解释） | 1 |
| `index-constituent` | 沪深300/1000 成分，按月存 | 2–3/月 |
| `snapshot` | **只用来拉基准总股本**（一次，分批） | 不做每日 |

**不要**指望用它每天拿全市场市值：`snapshot` 是单只一次，5000 只/天在 200 次额度下不可行。
市值请用 `收盘价 × 总股本`，总股本从 `adj_factor` 的送股/配股事件递推。
