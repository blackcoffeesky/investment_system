# 变更台账

---

# v5.42（2026-11-14）：分红率 → 股息率，`dividend_annual` 彻底摘掉

## 一、为什么要换

`dividend_annual`（全市场「分红总额 / 归母净利润」）是 causual 里**唯一**硬依赖
这张表的地方：`PAYOUT → PAYOUT_RATIO → 供需层「增长转化 · 分红/净利润」一行`。
数据层要删这张表（库里没有逐股财务，留着也拼不出新值），一删
`series_map` 直接抛 `DatabaseError: no such table`，**整张面板连卡片一起挂**。

而且那行本来就是**伪量化**：库里只有 5 个年度点（4 个不同值），进 `pair_view`
按月 resample 后却给出「位置 23 分位 / 边际 31 分位」这种看起来很精确的读数。

## 二、换了什么

| 项 | 旧 | 新 |
|---|---|---|
| 数据 | `dividend_annual.payout_ratio`（落表） | `DataPort.dividend_yield()`（现算，不落表） |
| 指标 | 分红/净利润（分配率） | 股息率（市值加权，%） |
| 分组 | 增长转化 | **股东回报 / 估值**（独立小节） |
| 边际 | yoy 分位 | **去掉**（11 个年度点算不出可信边际） |
| 位置分位 | 60 期月频窗口 | `freq="YE"` + `min_periods=8`，在**年度序列**上算 |

## 三、实现细节（都是踩过的地方）

1. **前视**：`dividend_yield(Y)` 用的是年 Y 内的除息日 + 年末收盘价，所以 Y 年
   的数据要到 Y+1 才完整。装配点把每个年度值对齐到 **Y+1-01-01**，面板只 ffill
   → **Y 年看到的是 Y-1 的值**。
2. **一次拿全表**：`dividend_yield()` 要扫 `stock_daily`，约 3.7 秒。
   装配点只调一次，不按年循环。
3. **期别不许落在未来**：年频 resample 会把当年标成 `2026-12-31`，而面板只到
   11-10。`pair_view` 的 `asof` 现在**钳到数据最后一天**——顺手也修掉了月频行
   显示 `2026-11-30`（未来月末）的老问题。
4. **卡片不显示边际**：`pair_view(mom_col=None)` → `mom_off=True`；整组都没有边际
   时，渲染层**不出边际两列**，而不是摆一列「—」让人以为数据缺了。
5. **依赖自动收缩**：`REQUIRED_TABLES` 是从 `panel.ASHARE` 推导的，删掉 `PAYOUT`
   映射后 `dividend_annual` 自动消失（28 → 27 张表）；`freshness.WATCH_MACRO`
   里也一并删掉。

## 四、测试

- `test_dividend_annual_no_longer_required`：守卫「已彻底不依赖」（映射被加回来就红）
- `test_dividend_yield_reaches_panel`：派生量真的进了面板、是百分数、且新鲜
- `test_ashare_required_tables_have_data` 的注释同步（不再拿 `dividend_annual` 举例）

## 五、遗留（不在本模块）

`service/ops/states/causal.py::_load_series` 是**另一个装配点**，它只调
`data.series_map()`，不装派生量 → **web/ops 生成的卡片上股息率那行会显示「无数据」**。
已报 orchestrator（跨目录，未改）。

---

# v5.41（2026-11-14）：新鲜度监控复活 + 派生列去 inf

## 一、`WATCH_MACRO` 的表名全过期 → 宏观层过期永远不会被报出来

`freshness.WATCH_MACRO` 写着 `cn_cpi / cn_ppi / cn_m2 / cn_exports / cn_social_fin`，
但数据层早就改成 `cpi_yoy / ppi_yoy / m1_yoy / m2_yoy / exports_yoy`。
`check()` 里是 `if t not in idx.index: continue` —— **8 个名字只命中 3 个，
剩下 5 个静默跳过**。后果不是报错，而是"没有过期表"这个结论永远成立：
你以为有人在看，其实没有。

同一批过期名字在 `pipeline._month_of()` 里还有**第二份副本**，影响更大：
A 股的月频卡归档月实际由 `cb_holdings` / `gold_etf_holdings`（**黄金的表**）
决定，凑巧都是 2026-09-30 才没露出来。

| 改动 | 说明 |
|---|---|
| `MONTH_TABLES` | 月/季频宏观表清单，**只有一份**；`WATCH_MACRO = MONTH_TABLES + ("dividend_annual",)`（年频不能进归档月计算） |
| `_period_end("Y")` | 新增：上年末算"已结束的年度"。原先没有这一支 → 年频表永远 `continue` |
| `scoped(tables, required)` | 按 `pkg.REQUIRED_TABLES` 收窄。`metadata()` 是全局的，不收窄的话 A 股卡会报「gold_cpi_surprise 停在 …」 |
| `check()` 告警 | 清单里的名字在 metadata 找不到时 **stderr 打印**，不再静默 `continue` |

**行为变化**：A 股月频卡的归档月 `2026-09` → **`2026-10`**（现在由 A 股自己的
PMI/CPI/PPI/insider 决定，`macro_2026-10.md`）。

## 二、`SIZE_FLOW` 的 42 个 `±inf`

ETF 上市前份额是 0，`pct_change()` 在首日给出 `inf`（实测 42 个，全在 2016 年初）。
inf 本身不报错，但一旦进 z-score / 分位就把整列污染成 inf 或 nan。
`_derived` 里四条派生量统一过 `_finite()`（±inf → NaN）。

## 三、测试

新增 `tests/test_freshness_watchlist.py`（5 条）：

- 清单里每个名字都存在于 `DataPort.tables()`（**故意 fail-closed**：删表/改名要红）
- 清单里每张表的 `freq` 都能被判过期（W / SPARSE 不行，防止"加进清单也不生效"）
- `check()` 真的能抓出过期的月/季表，且不误报新鲜的
- `freq="Y"` 能被检查（不是永远跳过）
- 清单写了不存在的表时**必须出声**

`tests/test_causual_panel_derived.py` 的 `_bad_derived()` 增加 `±inf` 判定。

---

# v5.40（2026-11-14）：ETF 代码归一，派生列复活

## 一、bug：四列派生量整体消失，且全程无报错

数据层 2026-11-14 把 ETF 代码从裸码 `510300` 统一成带前缀 `sh510300`，
`causual_assets/ashare/panel.py::_derived` 有三处按裸码做字符串匹配：

```python
w.rename(columns=lambda c: c[1:] if c.startswith("c") else c)   # 只为旧 c510300 设计
cols = [c for c in GROUPS[g] if c in w.columns]                 # GROUPS 里是裸码
```

后果链条（**每一环都不报错**）：

1. `group()` 全部返回 `None` → `SIZE_FLOW` / `ETF_DISPERSION` / `NATIONAL_ETF` /
   `NATIONAL_ETF_EVENT` 四列不进面板；
2. `sum([])` 抛的 `AttributeError` 被 `except Exception: return out` 吞掉；
3. `TREATMENT_COL["NATIONAL_ETF"]` 指向的 `NATIONAL_ETF_EVENT` 不在面板里 →
   `signal_layer.build_bundle` 里 `tcol not in df.columns` 直接 `continue`
   → **国家队这条边整条从卡片消失**（config 里正式声明的节点悄悄没了）。

实测：修复前卡片可见边 **9 条**，修复后 **10 条**，差的正是
`NATIONAL_ETF->CSI_ALL`。

## 二、修法

| 改动 | 说明 |
|---|---|
| 新增 `panel.bare_code()` | 只取数字、末 6 位：`sh510300` / `sz159919` / `c510300` / `510300` / `510300.SH` → `510300` |
| 列名归一 | `_derived` 里先把列名过 `bare_code` 再建宽表，`GROUPS` 仍是裸码（两侧同口径） |
| 归一后重复列告警 | 同一代码出现两次（`510300` + `sh510300`）时打印，只保留最后一列 |
| 不再静默降级 | 原 `except: return out` 保留（下游还能跑），但**打印原因**；`wide` 为空时提前返回，不再靠 `AttributeError` 兜底 |

## 三、修掉一个无关的 KeyError（CLI 出卡最后一步）

`causual_core/pipeline.py` 两处用了 `spec['label_signature']`，而签名实际在
`spec["data"]["label_signature"]`（同文件 54 行就是对的）。`quiet=True` 的测试
路径不打印，所以一直没暴露；`python -m causual.causual_core.cli daily` 会在
**卡片已经全部写完**之后崩在最后一行 print 上（退出码 1）。

## 四、回归测试

新增 `tests/test_causual_panel_derived.py`（5 条）：

- `REQUIRED_TABLES` 每张表都真的取到数据（**故意 fail-closed**：数据层删表要立刻红）
- 四个派生列存在且最后有效日在最近交易日 5 个交易日内
- `bare_code` 各种写法的归一
- 每条边的 treatment / source 列都在面板里（防"整条边被静默跳过"）
- **反向验证**：把 `_derived` 打桩成返回 `{}`，上面两种判定必须变红

---

# v5.39（2026-11-04）：日频卡与月频卡分家

## 一、为什么拆

宏观变量是低频的，混在日频卡里会让人习惯性跳过——**即使宏观信息更重要**。
拆成两张卡，各自按自己的节奏更新。

| 卡片 | 内容 | 文件名 |
|---|---|---|
| 日频卡 | 市场状态 + 信号层归因（α） | `card_YYYY-MM-DD.md` |
| 月频卡 | 结构层 + 供需层（位置 × 边际） | `macro_YYYY-MM.md` |
| 详情 | 日频：模型/边/β/滞后/贡献/新鲜度；宏观：供需明细 + 口径说明 | `detail_*` / `macro_detail_*` |

`narrative.build_card` / `build_detail_card` 改为**返回 `Section` 列表**，
调用方用 `narrative.render(sections, keep=(...))` 按需挑选或合并。
早期版本用 `card_split.py` 在文本层按 `## N.` 切分，已删除——
结构化返回比事后正则拆分可靠。

## 二、重命名

```
signal.py    → signal_layer.py
top_layer.py → macro_layer.py
```

## 三、月频卡按"数据能支撑的最新月"归档

`_month_of()` 取宏观表（CPI/M2/出口/央行持仓…）的 `max(end_date)` 所在月。
11-01 时 CPI 还停在 9 月，这时候不该出「11 月宏观卡」。

幂等按月份：`macro_YYYY-MM.md` 存在就跳过，**文件本身就是"上次生成"的记录**，
不需要额外状态。

## 四、卡片指纹（判断是否需要重生成）

每张卡旁边写一份 `*.meta.json`，记录生成时各表的 `end_date`：

```json
{"tables": {"csi_all": "2026-11-04", "vix": "2026-11-02"}}
```

列表页比对当前值，有差异就标「需重生成」。

**为什么不用 lag_days 判断过期**：卡片日期 = 数据截止日，两者天然对齐，
对齐判定永远不报警；而"VIX 补了 11-01~11-03 的数据但没到 11-04"这种情况
对不上日期、却让卡片内容过时。指纹能抓住它。`lag_days` 只用来提示
"这张表本期预计什么时候发布"，不产生告警。

## 五、月频新鲜度

`freshness.WATCH_MACRO`：CPI/PPI/M2/出口/社融/央行持仓/ETF 持仓/分红。
月频卡用自己的清单，不再显示日频那几条（vix/usdcny 跟宏观卡无关）。

## 六、状态机

```
fetch → manual_check → validate → generate_cards（日频）
                                └→ generate_macro（月频）
```

两者互不阻塞：日频卡失败不该拦住月频卡。

## 七、验收

```
39/39 测试通过
ashare: card_2026-11-04 / macro_2026-09 / detail / macro_detail
gold:   同结构
/cards?type=daily|macro 均 200
```

---

# v5.38（2026-11-04）：文档分层 + 测试污染修复

## 一、文档重构

| 文件 | 变化 |
|---|---|
| `quant/README.md` | **新建**：模块地图、依赖方向、数据流全景、日常命令 |
| `causual/doc/readme.md` | 瘦身为 causual 自己的文档；新增 §2.1「与 service 的接口」 |
| `causual/doc/DESIGN.md` | §2 改为「causual 在整体中的位置」；新增 §9 对外接口 |
| `service/DESIGN.md` | **新建**：逐文件职责、状态机语义、HTTP 接口、扩展方式 |
| `datahub/{README,DESIGN}.md` | 去掉已删除的 `import_panels`；`db.py` → `fin/read.py` |

## 二、清理

`causual/trash/`（diag_supply / diag_xau / imf_fetch / long_regime）和
`tests/_tmp.txt` 归档到 `trash/legacy_20261104/`。

## 三、测试污染（重要）

`test_ops_page_renders_with_grouped_messages` 直接往**真实库**插了 3 条
"告警 0/1/2"，并把 `manual_check` 置成 `waiting_ack`——所以每次跑完测试，
运维页都会冒出假告警。

改成用临时库 + monkeypatch `service.app.DB_PATH`，并清理了已污染的 3 条记录。

## 四、运维页分组可读性

分组标题补上 key 列表（`cb_holdings、insider_net`），摘要改成
「第一条全文（另有 N-1 条，展开可见）」，不再是「告警 0 等 3 条」。

## 五、新增端到端测试

`tests/test_e2e.py`：临时库建表灌数 + 卡片接口在临时目录下的检索/渲染。
共 **37/37 通过**。

---

# v5.37（2026-11-04）：锁问题根治

## 一、为什么 WAL 之后仍报 locked

两个残留：

1. **每次连接都执行 `PRAGMA journal_mode=WAL`**。切换日志模式要**独占锁**，
   别的连接正在写时这一步会卡住，超时就报 `database is locked`。
   改成：先读当前模式，已经是 wal 就跳过；切换失败也不致命。
2. **`Manager.__init__` 里跑 `_recover()`**——每次打开 `/ops` 都会 new 一个 Manager，
   于是**页面加载也在写库**。后台 fetch 正在写时打开页面就撞锁。

## 二、改法

| 位置 | 改动 |
|---|---|
| `datahub/core/conn.py` | 先设 busy_timeout，再按需切 WAL，切换异常吞掉 |
| `datahub/fin/update.py`、`intake/cli.py` | 改用 `core.conn.connect()`（原先裸 `sqlite3.connect`，只有 5 秒超时） |
| `service/ops/manager.py` | `_recover` → 公开的 `recover()`；构造时只读快照 |
| `service/app.py` | 进程启动时调一次 `recover()`；`run()` 开头也调一次 |

## 三、验证

```
混合读写压测（3 写 × 15 次 + 3 读 × 30 次）：无错误
长事务占锁 2 秒时写 ops：busy_timeout 生效，无错误
35/35 测试通过
```

---

# v5.36（2026-11-04）：新鲜度参考日 / WAL / 模板 500

## 一、新鲜度用卡片日期，不用"今天"

11-03 的卡片在 11-04 重新生成时，`freshness` 拿 11-04 当参考，于是所有日频表都被判
"滞后一天"。改成传 `today=when`（卡片日期）。

修复后 11-03 的卡正确显示：usdcny/usd_index 停在 10-30（真滞后 4 天）、
vix/fed_rate 停在 11-02（滞后 1 天），而 csi_all 不再误报。

## 二、`database is locked`

`journal_mode=delete` + `busy_timeout=5000`：后台 fetch 写 datahub 时，
Manager 写 ops_state、页面读，写写互斥 5 秒就抛错。

`datahub/core/conn.py` 的 `connect()` 统一加：

```python
PRAGMA journal_mode=WAL
PRAGMA busy_timeout=30000
PRAGMA synchronous=NORMAL
```

并发压测（4 线程 × 20 次写）无错误。

## 三、运维页 500

`{% for m in g.items %}`：Jinja 的 `.` 优先取属性，`g.items` 是 dict 的方法而不是键。
改成 `g['items']`。

## 四、`/health` 自检

```json
{"ok": true, "modules": ["causual"], "journal_mode": "wal", "busy_timeout": 30000}
```

两个字段足以判断"WAL 有没有生效""是不是用了裸 connect"。

## 五、测试

新增 3 个（新鲜度按 asof、月频 lag 不误报、运维页多消息分组渲染），
共 **35/35 通过**。

---

# v5.35（2026-11-03）：数据新鲜度 + 重生成卡片 + 先验放大

## 一、数据新鲜度（causual 层）

新增 `causual_core/freshness.py`：按 `metadata.freq` + `lag_days` 推算每张表
**期望的最新期**，与实际 `end_date` 比。日频差一个交易日就算过期，月/季按 lag 放宽。

卡片页首会显示：

> ⚠ **数据滞后**：vix 停在 2026-10-30，期望至少 2026-11-03；… 等 6 张表

原来只看 `csi_all` 的日期，指数更新了但 VIX/汇率/两融没更新时，卡片看起来"都是新数据"。

## 二、重新生成卡片

- `causual_cards` / `CardService.regenerate(asset, date)`
- `POST /card/{asset}/{date}/regenerate` → 删旧文件后重跑
- 卡片列表每行加「重新生成」按钮

## 三、运维页消息分组

同一状态的多条告警在**展示层**折叠成一组（`<details>` 展开），
`Message` 结构不变——一条消息仍对应一个 `(key, period)`，那是静音的基础。

## 四、分红率修复

`_payout_series` 里 `from datahub.fin import read as db` 后调用 `db.read(...)`，
`read` 是函数不是模块 → 每次都抛异常 → `PAYOUT_RATIO` 全 NaN。改成直接 `read(...)`。

## 五、先验放大

`signal.PRIOR_SCALE = 2.0`：`contrib = magnitude × PRIOR_SCALE × …`。
只影响 α 量级，不影响 β 估计；改回 1.0 即还原。

当期 α 由 −0.01 变为 **+0.05**。

## 六、验收

```
32/32 测试通过
卡片：8 / 19 张表，0 条未解析
```

---

# v5.34（2026-11-03）：出卡状态修复 + 后台运行

## 一、`generate_cards` 报"没有注入 CardService"

`service/app.py` 的 `_manager()` 构造 Manager 时没设 `mgr.extra`，只有测试脚本里设了。
网页触发的运行因此拿不到 CardService。已注入：

```python
mgr = Manager(STATES, get_data_service(), DB_PATH)
mgr.extra = {"cards": CardService()}
```

## 二、点"重跑"后状态变 pending 就没了

根因：`/task/run` 是**同步**处理，`fetch` 一次要跑几分钟（FRED/CFTC 走网络），
HTTP 请求一直挂着，浏览器拿不到响应——用户看到的就是"状态变 pending 然后没反应"。

改为**后台线程**：

- `POST /task/run` 启动 daemon 线程后立刻 303
- `/ops` 检测 `ops_run_log.finished_at IS NULL` → 显示"正在运行…" + 5 秒自动刷新
- 线程结束（含异常）释放锁，写 `log_run_end`

顺带把 CFTC 重试从 5 次×递增退避（约 275 秒）收敛到 3 次×2/6/10 秒。

## 三、验证

```
web 触发 → 立即 303 /ops?started=1
后台线程：run_log.status=running，state.fetch=running（页面可见）
generate_cards（走 web 的 _manager）→ 生成 ashare/gold 2026-11-03
32/32 测试通过
```

---

# v5.33（2026-11-03）：重跑 + 表单 bug + 场景测试

## 一、表单提交 bug（根因）

`ops.html` 的 submit 处理器把按钮 `disabled = true`。**被禁用的按钮其 name/value
不随表单提交**，于是 `action` 字段丢失，后端退回默认 `retry`——表现为
"点了『知道了』却报 INFO 不允许重跑"。

改为 `pointer-events: none` + 降透明度，不动 `disabled`。

## 二、整轮重跑

- `Manager.reset_all()`：done/snoozed/pending → pending，`attempts` 清零并存进 `last_attempts`；
  **拒绝**在有 blocked/waiting_ack 时执行（避免绕过确认）；**不清静音**
- `Manager.all_settled()` / `has_unacked()` 供界面判断
- `/task/run` 增加 `mode=run|rerun`
- `/ops` 按钮随状态切换：先处理 → 运行一次 → 重跑
- 跑完在页顶显示本轮摘要（读 `ops_run_log.summary`）

## 三、其他修复

- `attempts` 只在 blocked/waiting_ack 时 +1（日常重复运行不再涨）
- 静音期内补一条 INFO：「N 条告警在静音期内，不打断」
- `reset_all` 后必须 `_refresh()`，否则内存快照还是旧 attempts

## 四、测试

新增 `tests/test_ops.py`（19 个用例），总计 **32/32 通过**。
用例全部使用临时数据库，不碰真实运维状态。

---

# v5.32（2026-11-03）：静音逐条化 + 出卡状态 + 看门狗

## 一、状态机

| 改动 | 说明 |
|---|---|
| 静音逐条判断 | 之前只看第一条 WARNING，静音一张表等于静音整个状态 |
| 删 `SKIPPED_BY_ACK` | SKIP(once) 也记 `SNOOZED`——检查确实执行了 |
| 静音消息自动已读 | 不占待办；同一 (state,key) 的旧消息标 `superseded` |
| `ops_run_log` | 每次 run 记起止与结果；`/ops` 超 26 小时显示横幅 |
| `/task/run` 锁 | 非阻塞锁，抢不到 409；前端按钮 disabled |

## 二、新状态 `generate_cards`

`prefix = (fetch, manual_check, validate)`，数据层全过后出卡：

- 目标日期 = **真实数据最后一天**（`csi_all` / `gold_cny` 的 `index[-1]`）
- 幂等：已有卡跳过
- 单资产失败 → WARNING（key=资产，可静音）；全失败 → ERROR
- 通过 `ctx.extra["cards"]`（`CardService`）调用，不直接 import causual

## 三、卡片日期语义

`daily.run` 的 `when` 改为**面板最后一天**，不再是外推后的标签日。
卡片第 1 节新增「数据截止」一行；若标签外推超出，注明
「标签外推至 X（数据截止 Y）」。

## 四、修掉的 bug

- `narrative.py` 三张表的分隔行少一列（表头 8 列、分隔 7 列），
  Python-Markdown 直接拒绝解析，整表退化成文本
- `datahub.fin.update._ensure_meta` 没排除 `ops_snooze`/`ops_run_log`，
  导致 `check` 把它们当数据表读

## 五、验收

```
15/15 测试通过
run: fetch done → manual_check waiting_ack → ack(skip) → snoozed
     → validate done → generate_cards done（生成 2 张卡）
第二次 generate_cards → already_done（幂等）
/ops 200，/task/run 303，卡片 HTML：8 / 19 张表，0 条未解析
```

---

# v5.31（2026-11-02）：服务层 + 卡片静音 + HTML

## 一、datahub 拆成 core / fin / ops

```
datahub/
├── core/   conn.py（连接/事务）  schema.py（DATA_DDL / OPS_DDL）  errors.py
├── fin/    read / update / sources / check / intake / import_panels
└── ops/    OpsStore（状态 + 消息，单事务 save_result）
```

- `fin.tables()` 改成读 metadata → ops_* 永远不会混进金融表清单
- `ops_messages` 加 `run_id` / `key` / `period` / `ack_action`
- 新增 `ops_snooze`（按 (state,key) 静音）
- `datahub.ops` 不 import service（消息以 dict 传入）

## 二、状态机修复

| 问题 | 修法 |
|---|---|
| INFO 确认会改状态（把 DONE 打回 PENDING） | INFO 只允许 ACK，且不改状态 |
| `_recover` 自动重跑 | running → **blocked + UNEXPECTED_INTERRUPT**，用户 RETRY |
| 状态与消息两次 commit | `save_result()` 单事务 |
| `run()` 反复查库 | 开头取一次快照 |
| `SKIPPED` 无生产路径 | 删除；新增 `SNOOZED`（静音但下次仍检查） |
| 状态反向 import datahub | `DataService.covered_tables()` |
| 状态文件一状态一文件 | 合并成 `states/data.py` + `STATES` 注册表 |

## 三、静音（snooze）

SKIP 可带 `remind_at`：

| 值 | 含义 |
|---|---|
| `once` | 只跳过本次 |
| `days:N` | N 天内不再提醒 |
| `period_change` | 数据期变了才提醒 |

默认按状态声明：`manual_check=period_change`、`validate=days:7`、`fetch=days:1`。
静音期内的检查**照常执行**（状态记 `SNOOZED`），只是不打断。
运维页列出"已静音超 90 天"的条目当待办。

## 四、前端

- `service/app.py`：FastAPI + 鉴权/日志/错误中间件 + 模块挂载
- `service/templates/base.html`：导航 + 内容区 + 移动端 CSS（表格横滚）
- `/cards` 列表、`/card/{asset}/{date}` 渲染、`/ops` 运维页、`/task/confirm`
- 卡片目录改为 `output/{asset}/cards/YYYY/MM/`
- `causual.causual_cards`：`list_cards / get_card / to_html`

## 五、验收

```
15/15 测试通过
uvicorn 启动正常：/health 200、/cards 200、/card/... 200、/ops 200
/task/confirm → 303 回 /ops，snooze 记录写入
两支资产出卡，α = −0.01（与改前一致）
```

---

# v5.30（2026-11-01）：三个"伪手工"变量改回自动源

## 一、查证

旧 readme 的数据源表（第 133/135/137 行）与 `trash/gold_fetch.py` 的注释确认：

| 变量 | 真实来源 | 结论 |
|---|---|---|
| `CPI_SURPRISE` | FRED `CPIAUCSL` → 同比 − AR(1) 预期 | **自动** |
| `COT_NET_LONG` | CFTC Legacy：非商业净多头/未平仓量（%），周频 | **自动** |
| `ETF_FLOW` | WGC ETF 月报 `ETF_Flows_*.xlsx` 的持仓差分 | **文件解析** |

都不是手工录入。之前误标为 manual 是因为 `datahub/manual/` 里的 CSV 被当成了来源。

## 二、改造

- `cpi_surprise` → `datahub.sources.us_cpi_surprise`（FRED + 扩展窗口 AR(1)）
- `cot_net_long` → `datahub.sources.cot_gold_net`（CFTC Socrata，周二→周日对齐）
  与旧序列相关性 **0.9999**，逐期相差 ≤0.1pp
- `etf_flow` → 不再单独存：新增 `gold_etf_holdings`（WGC 月报的持仓水平），
  `ETF_FLOW = diff(持仓)`

## 三、一个坑：差分必须在原始频率上做

一开始我在日频面板上 `resample("ME").last().diff()`，结果：
- ETF 流入与旧序列相关性只有 **0.32**
- 季度购金多出一个"未来季度"（尾部重复）

原因：日频是 ffill 的，月末/季末落点依赖交易日，先 resample 再 diff 会错位一到两期。
改为**从库里的月/季频表直接 diff**，再把结果只落在真实观测日对应的交易日上。

修复后 ETF 相关性 **0.9989**，季度购金尾部干净。

## 四、即时读数（黄金）

| 指标 | 位置 | 边际 | 读法 |
|---|---|---|---|
| 央行总持仓 | 100% | 3% | 见顶信号 |
| 央行净购金 | 33% | 67% | 左侧机会 |
| 黄金 ETF 净流入 | 77% | 75% | 强势且仍在增强 |

## 五、测试

15/15 通过。

---

# v5.29（2026-11-01）：手工数据 → intake 层；央行购金改季度

## 一、intake 层

`datahub/manual/` 退休，改为：

```
datahub/intake/
├── raw/        # 原始文件，原样留档
├── clean/      # 解析后的标准 CSV（只放待写入/覆盖的行）
├── parsers/    # 一源一函数
├── registry.py # 源标识 → (parser, 表, 频率)
└── cli.py      # python -m datahub.intake <source> <raw>
```

`clean/` 承接原来的 `manual/`：列契约与库表一致；入库时 upsert，
**源方修订的旧日期会覆盖库里的值**。

## 二、央行购金改季度

| 项 | 旧 | 新 |
|---|---|---|
| 频率 | 月（IMF 差分 × 年度缩放 + 手工读数） | **季**（WGC 87 国篮子合计） |
| 库表 | `cb_gold_buy` + `cb_holdings` 两张 | **只有 `cb_holdings`** |
| 增持 | 单独存 | `cb_holdings.diff()` 现算 |

`AWAITED`（WGC 尚未收到该国数据）按上季度补；2026Q2 有 23 国未更新，
按 Q1 补后为 **32,111.2 吨**（Q1 为 31,994.3）。

原月度数据归档到 `quant_temp/cb_gold_monthly_archive.csv`。

## 三、验收

- `python -m datahub.intake wgc <xlsx> --load` 实测：42 行入库
- 黄金卡片：央行总持仓 位置 100% / 边际 3% → 见顶信号；
  净购金 位置 57% / 边际 68% → 强势仍在增强
- 15/15 测试通过

---

# v5.28（2026-11-01）：供需层改为「位置 × 边际」

## 一、为什么改

旧的「分位 + 同比 + 自相关」混了三件事：位置（多高）、变化（往哪走）、粘性（多稳）。
读表的人拼不出结论。改成两个正交维度：

- **位置**：这个指标在自身历史的什么位置（只给分位）
- **边际**：它在变好还是变差（只给分位）

两者构成 2×2，读法按指标方向分化——**不预设「高就是坏」**。

## 二、读法矩阵

| 位置 | 边际 | 正向指标（+） | 负向指标（−） |
|---|---|---|---|
| 高 | 改善 | 强势且仍在增强（拥挤度上升） | 压力持续加大 |
| 高 | 恶化 | 见顶信号 | 压力见顶（转好） |
| 低 | 改善 | 左侧机会 | 压力开始累积 |
| 低 | 恶化 | 右侧回避 | 压力出清中 |

方向为「不定」的指标（CPI）不做自动判读。

## 三、口径

| 指标 | 方向 | 位置口径 | 边际口径 |
|---|---|---|---|
| M1/总市值 | + | 原始值 | 同比 |
| ΔM1 | + | M1 三月滚动和 | M1 同比 |
| 净供给（融资−增持） | − | 滚动和 | 原始值 |
| 社零 | + | 当月同比 | 同比 MA3 一阶差 |
| 工业增加值 | + | 当月同比 | 同比 MA3 一阶差 |
| PMI | + | 原始值 | 环比差 |
| M1−M2 | + | 原始值 | 同比 |
| CPI | 不定 | 同比 | 同比 MA3 一阶差 |
| PPI / 出口 | + | 同比 | 同比 MA3 一阶差 |
| 分红/净利润 | + | 原始值 | 同比 |
| 大股东净增持 | + | 滚动和 | 原始值 |

**位置与边际用同一个分位窗口**（min(60, 可用期数−1)），否则两个分位不可比。

## 四、数据

新增 `retail_sales_mom`（社零当月同比）——累计同比带季节性，位置和边际都该用它。

## 五、测试

15/15 通过；两支资产出卡。

---

# v5.27（2026-10-31）：数据层注册表化 + 消费侧彻底解耦

## 一、datahub：`sources.py` 一统

`source.csv`、`collect.py`、`calendar.py` 全部退休，合并为 `datahub/sources.py`：

```python
@dataclass(frozen=True)
class Source:
    table: str; func: Callable; params: dict = {}
    kind: str = "auto"; enabled: bool = True; note: str = ""
```

- **取数函数签名统一**：`f(start, end, table="", **_)`，不支持区间就在函数内过滤
- **本地缓存依赖去除**：ETF 份额和年度分红改为「以库中已有日期/年份为缓存」，
  不再依赖 `data/cache/*.csv`
- **update.py 简化**：删掉 `_load_sources` / `_parse_params` / `importlib` / `inspect`，
  直接遍历 `SOURCES`，`func(start=, end=, table=, **params)`
- `|` 拆表逻辑保留在 update（一个函数返回多列 → 写多张单表，是写入职责）
- 交易日历并入 `check.py`（只有它需要判断休市）

## 二、causual：数据获取全部下沉到 assets

- `causual_core/data_adapter.py` 删除，拆成：
  - `causual_assets/common.py`：列名映射、按 `lag_days` 推后可观测日、面板组装
  - `causual_assets/ashare/panel.py` / `gold/panel.py`：各自的字段映射与派生列
- **`causual_core` 里不再有任何取数/读库代码**（已验证）
- `ashare/fetch.py`、`gold/fetch.py` 退休（不再导出 CSV）
- `pipeline.load_raw()` 直接 `panel.panel()`，无 CSV 回落路径

## 三、目录调整

`data/regime`、`data/snapshots` → **`causual/data/`**（模型产物跟代码一起走）。
`data/` 留给原始数据，且正在移出工作空间。

## 四、trash

```
trash/
├── ashare_afre.py
├── ashare_dividend.py
├── ashare_etf_shares.py
├── ashare_fetch.py
├── causual_core_data_adapter.py
└── gold_fetch.py
```

## 五、测试

15/15 通过；两支资产出卡；`datahub.update` 实测 2/2 成功。

---

# v5.26（2026-10-31）：数据采集下沉 datahub + 两个新数据源

## 一、采集逻辑下沉

`afre.py`、`etf_shares.py`、`dividend.py`、两个 `fetch.py` 全部移入 `trash/`。
采集搬到 `datahub/collect.py`：

- `collect_etf()` / `fetch_dividend_year()` / `refresh_dividend()`

原因：**数据层不能依赖上游包**。之前 `datahub.sources` 反过来 import `causual`，
方向是错的。

新的 `causual_assets/*/fetch.py` 只剩一件事：把 datahub 面板导出成旧宽表 CSV
（离线备份用），不再有任何抓取逻辑。

ETD 派生量（`SIZE_FLOW` / `ETF_DISPERSION` / `NATIONAL_ETF` / `NATIONAL_ETF_EVENT`）
现在在 `data_adapter._ashare_derived()` 里由 `etf_shares` 现算。

## 二、两个新数据源

| 表 | 来源 | 覆盖 | 说明 |
|---|---|---|---|
| `xau_usd` | [XAUS Gold API](https://xaus.com/api/v1/history) | 2021-11 ~ 至今（1258 天） | 免费无需 key；2021-11 之前保留旧值 |
| `world_tension` | [World Tension Meter](https://worldtensionmeter.com/api/) | 2026-03-23 ~ 至今（222 天） | CC BY 4.0，需注明出处 |

XAUS 与旧 FRED 序列重叠段相关系数 **0.9999**，最大绝对差 164 美元（不同报价源），
可以替代已停更的 `GOLDAMGBD228NLBM`。

**WTM 历史只有 2026-03 起**，无法替代 2016 年起的 `geopolitic_risk`，所以：
旧序列冻结保留，新指标单独成表 `world_tension`。

## 三、测试

15/15 通过；两支资产出卡。

---

# v5.25（2026-10-31）：公布滞后 `lag_days`（消除前视）

## 一、问题

数据层的日期是**数据所属期末**（2026-09-30 = 9 月 CPI），但 9 月 CPI 要到 10 月 9 日
才公布。旧面板靠"次月初才更新"近似这件事，仍然早于真实公布日（交易所月度统计
实际是次月中旬发布，旧面板在月初就用了）。

## 二、做法

`metadata` 新增 `lag_days`（公布滞后天数），适配层按它把日期推后再 ffill：

```
9 月 CPI 记在 09-30，lag=9  →  10-09 起可见
```

| 数据 | lag | 依据 |
|---|---|---|
| PMI | 1 | 次月 1 日 |
| CPI / PPI | 9 | 统计局每月 9 日左右 |
| M1 / M2 | 12 | 央行每月 12 日左右 |
| 社零 / 工业增加值 / 总股本 / 总市值 / 社融 | 15 | 次月中旬 |
| 出口 | 13 | 海关次月 13 日左右 |
| 央行购金 / 黄金 ETF | 30 | WGC 月报滞后 |
| 年度分红 | 90 | 年报汇总 |

**lag 是数据层属性，放在 metadata，不写死在适配层**——换数据源时只改库，不改代码。

## 三、效果

`2026-09-30` 的 CPI 现在到 `2026-10-09` 才进入面板；总市值等到 `10-15`。
旧面板是 `10-01`（CPI）和 `10-01`（市值）——两者都偏早。

## 四、剩余差异（已确认非 bug）

与旧面板逐日比对，`TOTAL_MKTCAP` / `M1_LEVEL` 等仍有大量差异，原因：
- **旧面板把次月 1 日当可用日**，真实公布日是次月中旬 → 新口径晚约两周，**这才是对的**
- CPI/PPI/PMI 的**数值**本身变了（换到月度统计接口）

## 五、测试

15/15 通过；两支资产出卡。

---

# v5.24（2026-10-31）：ETF/分红改读 datahub + 边重估

## 一、`etf_shares` / `dividend` 接入数据层

- `etf_shares.shares_wide()` 优先读 `datahub.etf_shares`（列名 `c+代码` → 还原代码），
  回落本地缓存 CSV
- `dividend.load()` 优先读 `datahub.dividend_annual`，回落缓存

## 二、ETF 补采后重估边

ETF 份额缓存从 969 行补到 **1350 行**（补了约 400 周历史），
`NATIONAL_ETF` 的 60 日滚动分位基准随之改变 → 重估了 ashare 全部边。

## 三、新旧面板 A/B（同一天，仅切换数据源）

| | 旧 CSV | 新 datahub |
|---|---|---|
| 状态 | 窄幅震荡 | 窄幅震荡 |
| 激活机制 | 5/8（关联 1） | 4/8（关联 0） |
| α | −0.10 | −0.01 |
| ΔM1（3 月滚动） | −25,573 | −22,516 |
| PMI 分位 | 77% | 75% |

差异来源：
1. **数据修正**：CPI/PPI/PMI 换到月度统计接口（PPI 由 −3.6% → +4.6%），
   `equity_fin_monthly` 改为从人行表直接取（月份更多）
2. **月频水平序列的时点**：`TOTAL_MKTCAP`、`M1_LEVEL`、`TOTAL_SHARES` 在旧面板里
   记在"次月初"，数据层记在"月末"——**还有约 1 个月的错位没对齐**

## 四、`missing` 归入 datahub

`missing` 表在 `datahub.sqlite` 内，由 `datahub/check.py` 管理（`ack` / `missing_table`）。

## 五、已知问题

月频水平序列（`TOTAL_MKTCAP`/`M1_LEVEL`/`M2_LEVEL`/`TOTAL_SHARES`）的可用时点
比旧面板早约一个月。要么在 metadata 加 `lag_days` 由适配层推后，
要么接受差异——**待定**。

## 六、测试

15/15 通过。

---

# v5.23（2026-10-31）：接入 datahub 数据层

## 一、数据来源切换

`ashare` / `gold` 的 `load_raw()` 改为**优先读 datahub**，缺失时回落旧 CSV；
`CAUSUAL_DATA_SOURCE=csv` 可强制回落（迁移期对照）。

适配层 `causual_core/data_adapter.py` 负责两件事：
- 数据层列名（`csi_all`、`m1_level`）→ 业务层旧名（`CSI_ALL`、`M1_LEVEL`）
- 月频月末一行 → 日频 ffill 宽表

**这两件事都是消费方的习惯，不反向污染数据层。**

派生列（`SIZE_FLOW` / `ETF_DISPERSION` / `NATIONAL_ETF` / `NATIONAL_ETF_EVENT`）
在 `_ashare_derived()` 里由 `etf_shares` **现算**，不再入库。

## 二、数据修复

| 问题 | 处理 |
|---|---|
| 六个美联储序列缺 2026-09-25 与 10-01~10-06 | 重跑 FRED 补齐 |
| `xau_usd` 的 FRED 序列停更 | 停用该源，标"需换源" |
| `geopolitic_risk` 无可用 FRED 序列 | 停用，标"待换源" |
| `etf_shares` 2121 个空洞 | 补采到 1350 行，空洞清零 |

`db.check()` 现在用**交易日历**（SSE/NYSE）判断"该有的日期"，正常休市自动排除；
报告分「近期缺」（90 天内）与「历史缺口」两档。

## 三、`gold_cpi_surprise` 的定性

验证：均值 0.048、标准差 0.42、范围 −1.21~1.51，与 CPI 水平相关性 0.016
——符合"实际 − 预期"的形态。

但**库里只有 CPI 实际值，没有市场预期**，删掉后无法重建。
按"去掉后能否用库中其他数据合成"的判据，它目前属于**原始数据，保留**。

若将来把预期值也入库（`cpi_forecast`），它就变成派生量，应当删除。

## 四、当期读数变化

| 资产 | 改前 α | 改后 α | 原因 |
|---|---|---|---|
| ashare | −0.13 | **−0.02** | CPI/PPI/PMI 换到月度统计接口（PPI 由 −3.6% 修正为 +4.6%）+ 补齐 6 天 |
| gold | — | +0.51 | 同上，宏观口径修正 |

## 五、测试

15/15 通过。

---

# v5.22（2026-10-30）：修数据源（CPI/PPI/PMI 停更）+ 补回增长转化

## 一、为什么 CPI / PPI / PMI 停在 2025 年

**用错了接口。** 之前用的是 `macro_china_cpi_yearly` / `macro_china_ppi_yearly` /
`macro_china_pmi_yearly`——这些是**财经日历的"预测值/公布值"序列**，不是统计局的月度
数据，数据源在 2025 年下半年就停止更新，所以卡片上"期别"一直显示 2025-08。

改用月度统计接口：

| 指标 | 新接口 | 列 |
|---|---|---|
| CPI | `macro_china_cpi` | 全国-同比增长 |
| PPI | `macro_china_ppi` | 当月同比增长 |
| PMI | `macro_china_pmi` | 制造业-指数 |

更新后全部到 **2026-09**，而且数值变化很大：

| 指标 | 旧（日历接口） | 新（月度统计） |
|---|---|---|
| CPI | 0.00% | **0.87%** |
| PPI | −3.60% | **+4.60%** |
| PMI | 49.40 | **50.10** |

PPI 从"深度通缩"变成"正增长"——这是**结论级别的修正**。

**工业增加值、出口仍是 2025-08**：akshare 没有对应的月度统计接口，只有日历接口。
这条待补，需要换数据源（统计局官网或 Wind）。

## 二、补回「增长转化」

v5.19 精简时误把它移到详情卡，现补回主卡第 3.4 节：

| 指标 | 口径值 | 同比 | 分位 | 自相关 |
|---|---|---|---|---|
| 分红 / 净利润 | 29.45% | −6.4% | 48%（同比·45月） | 0.94 高粘性 |
| 大股东净增持（3 月滚动） | −880.53 亿 | −37.4% | 57%（水平·60月） | 0.93 高粘性 |

## 三、去掉「与股市同期相关」列

实测所有宏观指标的同期相关都在 ±0.2 以内，没有区分度。**但 corr 仍在数据里计算**，
只是不展示，以后要排查时可直接读。

## 四、测试

15/15 通过。

---

# v5.21（2026-10-30）：移除条件均值差，改「同期相关 + 自相关」

## 一、为什么删掉 60/120/180 日条件均值差

用户指出：M1−M2 与 PPI 的方向呈相反关系，不是因为它们领先，而是**因为它们是同期的**。
传导快、指标粘性高（PPI 的 AR1 = 0.99），于是"未来收益对当前指标"的条件均值
实际上测的是**因素粘性 + 股市均值回归**的混合体，不是传导。

这在方法上是硬伤：**高自相关序列在任何滞后阶都会测出相关，但那是序列自身的延续性。**

所以卡片第 3 节删掉「传导结构」列。detail 里保留数值，但表头改为
「参考：存在均值回归混淆，不作为传导度量」。

## 二、改成三样能说清的

| 列 | 定义 |
|---|---|
| 同比(趋势) | 指标自身的变化方向（五类口径见 v5.18） |
| **与股市同期相关** | 同一个月「指标值 vs 当月收益」的相关系数（近 60 个月），**同期、非领先、非因果** |
| **自相关(AR1)** | 指标自身的 1 阶自相关（近 60 个月）；>0.8 高粘性 / 0.5~0.8 中 / <0.5 低 |

卡片注明：*宏观因素传导可能需要时间，不同因素传导速度不一致，且与因素本身粘性
高度耦合，目前无法定量给出。*

## 三、README：分层依据改为「因果深度」

原文写成"按时间尺度分层"，容易让人以为分层是为了对齐频率。改为：

> 分层依据是**因果深度**，不是数据频率。供需层与结构层更接近股市涨跌的深层结构；
> 层次越深，数据的自然频率通常越低——**频率是深度的副产品，不是分层依据**。

## 四、当期读数（A 股）

| 分类 | 指标 | 同比 | 分位 | 同期相关 | 自相关 |
|---|---|---|---|---|---|
| 增长 | 社零 | +1.1% | 21% | −0.07 | 0.92 高 |
| 增长 | 工业增加值 | +5.7% | 77% | −0.19 | 0.58 中 |
| 增长 | PMI | → +0.00 | 56% | −0.02 | 0.37 低 |
| 增长 | M1−M2 | −3.8% | 80% | −0.07 | 0.95 高 |
| 通胀 | CPI | +0.0% | 46% | −0.04 | 0.97 高 |
| 通胀 | PPI | −3.6% | 28% | −0.09 | 0.99 高 |
| 外需 | 出口 | +7.2% | 62% | −0.03 | 0.93 高 |

**所有宏观指标的同期相关都极弱（|r| < 0.2）**——这本身就是结论：
A 股月度涨跌与这些慢变量基本同期无关。这也解释了为什么条件均值差会给出误导性结果。

## 五、测试

15/15 通过。

---

# v5.20（2026-10-29）：供需层精简 + 分位基准标注 + M1−M2

## 一、卡片精简

- **3.1 供需总量**：只留 `M1 / 总市值` 一行
- **3.2 供需增量**：只留三行 —— `ΔM1`、`净供给`（融资−增持，不拆分子）、`增量比`
- 总股本、总市值、股票融资、大股东净增持的原始数值**移到详情卡**（第 8 节「原始序列」）

## 二、宏观环境合并成一张表

第一列分类（增长/通胀/外需），第二列指标：

| 分类 | 指标 |
|---|---|
| 增长 | 社零、工业增加值、PMI、M1同比−M2同比 |
| 通胀 | CPI、PPI |
| 外需 | 出口 |

- **去掉 M1 同比、M2 同比**，改为 `M1同比 − M2同比`（正 = 资金活化改善）
- PMI 显示具体数值，并加「方向」列（MA3 较 3 个月前的差，如 `→ +0.00`）

## 三、分位列标注基准（回答"分位是谁的分位"）

| 类型 | 同比的除数 | 分位基准 |
|---|---|---|
| 累计同比（社零/工业增加值） | 官方口径（今年累计 / 去年累计 −1），同比列就是它本身 | **同比序列** |
| 同比+MA3（CPI/PPI/出口/M1−M2） | 官方当月同比（除数是**去年同月**） | **同比序列**（MA3 后的） |
| 扩散指数（PMI） | 不算同比 | **水平序列** |
| 滚动和（ΔM1/净供给） | 本期滚动和 / 去年同期的滚动和 − 1 | **水平序列** |
| 存量（M1/总市值） | 本期 / 去年同月 − 1 | **同比序列** |

卡片每个单元格写成 `54%（同比·60月）` / `20%（水平·60月）`，不用再猜。

## 四、其它

- 新增 `SUPPLY_RATIO`、`M1M2_GAP` 两列
- `describe_series` 增加 `cat` 参数（宏观分类）与 `kind="ratio"`
- 测试 15/15

---

# v5.19（2026-10-29）：供需层四小节 + 同比列 + 传导结构 + 社融表

## 一、社融官方表接入

`data/manual/social_financing/` 下的人行 Excel（2016–2026，117 个月）：

- `YYYYflow.xlsx`：增量表，取「非金融企业境内股票融资」（**亿元/月**）
- `YYYYstock.xlsx`：存量表，取「社会融资规模存量」「非金融企业境内股票」（**万亿元 ×10000**）

两个坑：
1. 单位不同（亿元 vs 万亿元）
2. **月份被截断**：`2025.10/11/12` 常被读成 `2025.1`。按出现顺序判定月份，不靠字符串解析。

## 二、三个派生指标

```
净供给      = 股票融资增量 − 大股东净增持     （融资 +，增持 −）
供需总量比  = M1 存量 / 总市值（12 月 MA）     （总市值代替流通市值）
供需增量比  = ΔM1 / 净供给（3 月滚动和）
```

**必须在月频上算再 ffill 回日频**——直接在日频 diff 会恒等于 0（日频是 ffill 的）。

新增 `M1_LEVEL`、`M2_LEVEL`（亿元）。

## 三、每个指标两列：口径值 + 同比

| 类型 | 同比 | 分位基准 |
|---|---|---|
| 累计同比 | 官方累计同比 | 当前累计同比在过去 N 个月中的位置 |
| 同比+MA3 | 官方当月同比的 MA3 | MA3 在过去 N 个月 MA3 中的位置 |
| 扩散指数（PMI） | 不算 | 当前值在过去 N 个月中的位置，另看 MA3 方向 |
| 滚动和 | 滚动和/去年同期 − 1 | 当前滚动和在 N 个月中的位置 |
| 存量 | 当月值/去年同期 − 1 | 当前**同比**在 N 个月同比中的位置（水平分位另列） |

**踩坑**：`_yoy12` 返回比例，直接按 `%` 格式化会显示成 `+0.0%`。统一乘 100。

## 四、传导结构

60/120/180 日条件均值差压缩成一句判定（交替直接说"不稳定"，不硬解释）：
`持续有利/不利`、`短中期X、长期Y`、`短期X、中长期Y`、`不稳定，无清晰传导结构`。

card 只显示这一句 + 三个数字；三个窗口的明细移到 detail 第 3 节。

## 五、卡片

第 3 节拆成 3.1 供需总量 / 3.2 供需增量 / 3.3 宏观环境 / 3.4 增长转化。
表列：`指标 | 口径值 | 同比 | 分位(窗口) | 传导结构 | 期别`。

detail 新增第 3 节「供需层明细（60/120/180 日）」与第 8 节「口径说明」。

## 六、两融

`MARGIN_NET` = 余额差分（亿元/日），读数显示「净流入 +37 亿 / 净流出 −22 亿」，
不再是余额水平。边已重估。

## 七、README

更新到 v5.19：分层、四小节、五类口径、传导结构、日常命令、手工数据清单。

## 八、测试

15/15 通过。

---

# v5.18（2026-10-29）：供需层口径分型 + 宏观扩充 + 事件说明 + 改名

## 一、口径分型（核心改动）

供需层的原始形态差太多：社零是累计同比、CPI 是当月同比、净融资是流量、总股本是存量。
直接拿"当月值"比历史分位会被单月噪声主导（6 月刺激冲高、7 月回落）。

`describe_series(df, col, label=..., kind=...)` 支持四类：

| kind | 口径值 | 分位基准 |
|---|---|---|
| `cum_yoy` | 累计同比 | 当前累计同比在 N 个月累计同比中的位置 |
| `ma3` | 同比的 3 月移动平均 | 当前 MA3 在 N 个月 MA3 中的位置 |
| `roll3` | 3 月滚动和 | 当前滚动和在 N 个月滚动和中的位置 |
| `stock` | 当月值/存量同比 | 当前值在 N 个月中的位置 |

- 分位窗口**自适应**：`N = min(60, 可用月数 − 12)`，卡片标注实际用了几个月
- 条件均值差从 1 个窗口扩到 **60 / 120 / 180 日**（传导可能要几个月）

## 二、宏观数据扩充

新增 7 列：社零、CPI、PPI、M1、M2、工业增加值、出口。
固定资产投资无接口，跳过。

**坑**：各指标期别不同（CPI 比社零落后半年），必须逐列 `dropna()` 后再 ffill，
否则并集索引会把落后的列整段变成 NaN。

## 三、事件边

- 第 2 节事件行新增：**滞后期**（`窗口 1~5 日`）+ **激活状态**（`已激活 z=+3.49` / `未激活 z=-0.57`）
- `Edge.direction_unclear`：**放量冲击不再判"方向冲突"**，显示「关联·方向不定」。
  理由：放量多伴随大涨，`direction=-1` 本身不成立

## 四、改名

`regime_pipeline → regime_fit`、`regime_online → regime_infer`、`structural → top_layer`
（保持单文件，不拆分）。所有 import 已同步。

## 五、卡片

第 3 节表头：`指标 | 口径值 | 分位(窗口) | 未来60日 | 未来120日 | 未来180日 | 期别`

detail 卡新增**第 7 节「供需层口径说明」**，解释四类 kind 各自的列含义。

## 六、当期读数（A 股）

| 指标 | 口径值 | 分位 | 60日 | 120日 | 180日 |
|---|---|---|---|---|---|
| 总股本 | 79,655 亿股 | 100% | −1.3% | −2.5% | −2.7% |
| 市价总值 | 110.7 万亿 | 90% | −3.7% | −5.8% | −11.1% |
| 社零 | 1.10 | 21% | +0.0% | −0.2% | −2.5% |
| M2 | 7.30 | 18% | +1.7% | +3.2% | +3.3% |
| 工业增加值 | 5.70 | 77% | +3.5% | +5.2% | +8.1% |
| 股东回报 | 29.45% | 17% | −2.8% | −5.3% | −3.8% |

黄金：央行总持仓 32,170 吨（分位 100%，180 日 +10.7%）、净购金 117 吨/季（分位 67%）、
ETF 流入 255（分位 89%）。

## 七、测试

15/15 通过。

---

# v5.17（2026-10-28）：股东回报接入 / 黄金央行持仓

## 一、股东回报：全市场分红 ÷ 归母净利润

新增 `causual_assets/ashare/dividend.py`，用两个接口汇总：

```
分红总额 = Σ(现金分红比例/10 × 总股本)     ← stock_fhps_em(报告期)
净利润总额 = Σ(净利润)                      ← stock_yjbb_em(报告期)
分红率 = 分红总额 / 净利润总额
```

按年缓存到 `data/cache/dividend_annual.csv`（每年单独落盘，中断可续）。

已有年份：

| 年 | 分红总额（万亿） | 净利润（万亿） | 分红率 |
|---|---|---|---|
| 2021 | 1.74 | 5.34 | 32.7% |
| 2022 | 1.87 | 5.46 | 34.3% |
| 2023 | 1.96 | 5.41 | **36.3%** |
| 2024 | 1.65 | 5.25 | 31.5% |
| 2025 | 1.59 | 5.39 | **29.5%** |

**分红率 2023 年见顶后连续两年下滑**，今年分位只有 17%。这和结构层里
「分红与回购制度性改善」的判断**方向相反**——已在卡片里如实并列，不替任何一方圆场。

口径说明：分母用接口的"净利润"（含少数股东权益），不是严格归母；解读时按近似处理。

## 二、黄金供需层补齐

| 指标 | 说明 |
|---|---|
| 央行总持仓（吨） | `CB_HOLDINGS`，存量 |
| 央行净购金（吨/月） | `CB_GOLD_BUY`，流量 |

当期：总持仓 32,170 吨（同比 +0.8%，**历史最高**），净购金 39 吨/月（同比 −15.2%）。

存量创新高但流量在放缓——这两条要一起看才有意义。

## 三、其它

- 供需层"期别"改为取最后一次真正变化的月份（之前显示 ffill 后的当月）
- `refresh()` 改为逐年落盘：单次抓取可能要几分钟，中断后不必从头再来
- 测试 15/15

## 四、未完成

- 2016-2020 年的分红数据未抓全（2020 接口无数据，2016-2019 那次任务卡住被中止）。
  补齐后分位才有 10 年基准，现在实际只有 5 年。

---

# v5.16（2026-10-28）：股票总供给接入 / 结构层两条 / README 更新

## 一、股票总供给：东财「全国股票交易统计表」

新接口 `macro_china_stock_market_cap`，月频、2008 起、上交所+深交所分列：

```
TOTAL_SHARES = 发行总股本-上海 + -深圳   （亿股）
TOTAL_MKTCAP = 市价总值-上海 + -深圳     （亿元）
```

实测 225 个月（2008-01 ~ 2026-09）。当期：

| 指标 | 现值 | 同比 | 近 10 年分位 | 条件均值差 |
|---|---|---|---|---|
| 发行总股本 | 79,655.66 亿股 | +3.0% | **100%** | −1.3% |
| 市价总值 | 110.66 万亿元 | +4.0% | 97% | −3.7% |

股本总量处在历史最高位——这是一个之前完全看不到的维度。

## 二、社融接口修复

`macro_china_shrzgm` 已从"存量"改成"增量"、月份改成 `YYYYMM`。解析改为两者兼容，
增量按年内累计后再算同比。数据源本身只更新到 2026-04，卡片会如实显示期别。

## 三、结构层两条（草稿）

`data/manual/structural_state.csv` 写入两条：IPO 节奏、分红与回购。
judgement 前缀「待确认」，等人工核对后改。

## 四、供需层期别修正

`describe_series` 原来的"期别"取的是 ffill 后的日期（永远显示当月），
改为取**最后一次真正变化的月份**。修正后：

```
股票总供给 2026-09 ｜ 社融 2026-04 ｜ PMI 2025-09 ｜ 大股东 2026-09
```

一眼能看出哪些数据是陈的。**PMI 停在 2025-09 需要查**。

## 五、README

- 新增第 6 节「三层结构」：三层的分工、为什么分层、结构层为什么只有定性
- 第 7 节「卡片导读」重写为当前的四节结构（状态 / 结构层 / 供需层 / 信号层）
- 修正原有重复编号（两个 3、两个 6）

---

# v5.15（2026-10-28）：按时间尺度分层（结构 / 供需 / 信号）

## 一、为什么要分层

把"PMI 变化"和"VIX 跳升"放进同一个 DAG 是**范畴错误**：传导时间、数据频率、
样本长度、稳定性都差一个量级。混在一起算 α，等于假装它们是可通约的"边"。

新架构：

| 层 | 内容 | 频率 | 进 α |
|---|---|---|---|
| `structure` | 制度、政策态度 | 年/事件 | 否（只有定性） |
| `supply` | 股票/资金供给、价值、大股东态度、央行购金 | 月/季 | 否（描述统计） |
| `signal` | VIX、汇率、Shibor、两融、国家队、放量 | 日/周 | **只有这层** |

`Edge.tier` 区分三层；`Edge.layer` 保留原义（causal / associative）。
**两个名字必须分开**，否则"层"会同时指认识论地位和时间尺度。

## 二、结构层：MVP，人工维护

`data/manual/structural_state.csv`（列：date, area, judgement, evidence, falsifier, stance）。
卡片渲染成"判断 / 证据 / 证伪条件 / 更新日期"四列。

为什么只有定性：制度与政策一年变几次、不可重复，回归估不出 β。
强制每条判断带**证伪条件**，否则定性会变成事后怎么都能解释的叙事。

## 三、供需层：只做描述统计

`causual_core/layers.py::describe_series`：现值、同比、近 10 年分位、
**条件均值差**（指标高于/低于中位数时，未来 60 日均收益之差）。

明确标注"**不是因果估计**"——它只回答"高的时候后来涨得多还是少"。

## 四、边界调整

- `INSIDER_NET`（大股东态度）、`EQUITY_SUPPLY`（股票供给）→ `tier=supply`、退出 α
- 黄金 `CB_GOLD_BUY`（央行购金）→ `tier=supply`、退出 α
- `long_regime` **停用**（`prepare` 里注释掉）：它原本是给慢变量分组用的，
  这些边已挪到供需层，分组失去意义。实现保留在 `long_regime.py`

α 变化：A 股 −0.31 → **−0.14**（8 条信号边）；黄金 +0.78 → **+0.51**。

## 五、卡片

```
## 1. 市场状态
## 2. 结构层（制度与政策，仅定性）
## 3. 供需层（长期，描述统计）
## 4. 净指向分解（信号层，计入 α）
```

第 4 节顶部明确写："只覆盖信号层……所以「边全正但市场在跌」是正常现象：
缺的那部分在长期层。"

节号改为计数器生成（黄金没有结构层时不会跳号）。

## 六、架构

- `causual_core/utils/` 迁入 `calendar.py`、`zscore.py`，所有 import 改为新路径
- `causual_core/__init__.py` 改为 `from causual.causual_core.utils import calendar`
  （原来从包内再导子模块会形成循环导入）
- 测试 15/15

---

# v5.14（2026-10-27）：按长期状态分组估计当期系数

## 一、为什么分组

"熊市才回购、牛市才减持"——长期状态同时影响 treatment 和收益，是**混杂**。
把它当普通控制变量会使所有边都变成"给定趋势后的条件效应"，读不出方向。
但全交叉（日频 4 态 × 长期 3 态 = 12）样本不够，所以**每条边按自己的时间尺度分组**：

| 边 | 分组变量 |
|---|---|
| 慢变量（DL，月频） | 长期状态（月级别规则） |
| 快变量（DML，日频） | 日频 HMM regime（原有） |

`Edge.group_by` 可显式覆盖（`"none"` = 不分层，用于 treatment 在长期状态下游的边，
分层属过度控制）。

## 二、长期状态：规则判定，不用 HMM

`causual_assets/ashare/long_regime.py`：

| 组 | 规则 |
|---|---|
| bull | 收盘 > MA200 且 MA200 抬升 |
| bear | 收盘 < MA200 且 MA200 下行 |
| range | 其余 |

三组而非四组——月频只有 ~120 个点，再按"偏离 10%"切会压到每组 30 以下。
MA200 用**日线**算再取月末值（≈10 个月），与日频口径一致。

A 股实测：bull 82 / bear 46 / range 40（月）。

## 三、path 保持全样本，分组结果单独存

- `path` / `path_ci`：**全样本单一模型**（含 6 阶滞后 + 控制变量）
- `path_by_group` / `path_ci_by_group` / `n_by_group`：只估 **lag0**，
  且**不带滞后项、不带控制变量**（每组 ~20-60 个样本，多加一个参数就是噪音）

卡片明确标注"与上表是不同模型"，避免把两个模型的系数看成一条 path。

## 四、纯度必须跟着分组走

分组后的 β 和全样本的 CI 是两回事。用全样本的窄 CI 给分组的 β 打纯度，
会凭空给出 1.00。所以新增 `dl_purity_series()`：按当期所属分组取 CI，回退全样本。

当天效果（INSIDER_NET）：

| | β₀ | CI | 纯度 | 贡献 |
|---|---|---|---|---|
| 全样本 | −2.039 | [−3.28, −0.80] | 1.00 | +17bp |
| 长期震荡组 | +1.948 | [−3.33, +7.23] | 0.53 | −9bp |

α 从 −0.44 变为 −0.31。**分组暴露了"全样本那条显著的 −2.04 主要来自牛市"**：
bull −0.738（显著）、bear −0.233（不显著）、range +1.948（极宽）。

## 五、架构

- `causual_core/utils/` 新增，`calendar.py`、`zscore.py` 迁入；
  原文件保留为兼容转发（也顺手消除了 `calendar` 与 stdlib 同名的隐患）
- `LONG_REGIME` 在 `prepare()` 里生成，**不进** `TREATMENT_COL` / `confounders`
- 快照 `path_by_group` 一并持久化（schema 仍为 v3，字段向后兼容）

## 六、测试

15/15 通过。黄金尚未启用长期状态（无 `long_regime` 列，自动跳过分组）。

---

# v5.13（2026-10-27）：分布滞后改为"当期口径"

## 一、为什么改

卡片承担两件事，以前被混在一个 α 里：

1. **当期归因**——今天为什么涨/跌
2. **时间结构参考**——滞后效应有多长

旧实现把 lag1..N 的累加和塞进 α，读者无法分辨"这是当期归因还是未来预测"。
现在 **α 只做当期归因（lag0）**，滞后段降级为纯展示。

## 二、EdgeModel 瘦身

删除 `cumulative`、`cumulative_ci`、`beta_lagged`、`beta_contemp` 四个重复字段。
DL 边只留：

| 字段 | 内容 |
|---|---|
| `path` | lag0..lag{max_lag} 全部逐期系数（原来只存到 lag3） |
| `path_ci` | 各期 95% CI |
| `p_value` | **lag0** 的 p |

`beta_lead` 对 DL 边保持空（不再塞假标签）。

## 三、signal 拆成两段

```
agg_contemp = β_0 · z_t                → 进 α
agg_lagged  = Σ_{k≥1} β_k · z_{t−k}    → 只进详情卡"滞后效应参考"
```

- lag0 **不摊释放曲线**（`span=0`），否则慢变量的当期贡献会被无故打掉 57%
- `contrib_h` 仅供展示，不进任何 α

## 四、纯度与审计只看 lag0

- `_purity_of` 的 DL 分支：`purity(path[0], *path_ci[0])`，不再走 cumulative 回退链
- `sign_audit`：DL 边输出 `lag0` 行（进 α）+ `lag1..N` 参考行（无 CI）

## 五、快照升到 `edges-v3`

旧快照读进来会因字段缺失而算错，所以 `load` 直接拒绝：

```
ValueError: ... 的边快照是 edges-v2，当前需要 edges-v3 ...
请重跑：python -m causual.causual_core.cli edges --asset ...
```

## 六、卡片

- 头部只显示 `α(当期归因)`，并注明"只做当期归因，滞后效应见详情卡第 4 节"
- 第 2 节 DL 行：`当期 -2.0%（进 α）` + `参考 lag1+ +1.7%`
- 详情卡第 4 节标题改 `lag0 ~ lagN`，逐期表加 `含 0`、`纯度`、`用途` 三列
- 一致性标记改按 lag0 判定：`⚠ 当期反向（反向因果嫌疑）`，滞后段反向单独提示

## 七、当天结果对比（A 股 INSIDER_NET）

| 口径 | dose | 贡献 |
|---|---|---|
| 旧（lag1+，累计） | +1.703 × (−0.17) = −0.29 | **−4bp** |
| 新（lag0，当期） | −2.039 × (−0.17) = +0.35 | **+17bp** |

α(当期归因) 从 −0.22 变成 **+0.20**。符号翻转是预期内的：
内部人"跌了才增持"，当期系数天然为负，归因口径下"净减持"自然对应"当期偏正"。

**注意**：这不是预测信号。要看方向预测，得看 lag1+（详情卡第 4 节）。

## 八、测试

15/15 通过。

---

# v5.12（2026-10-26）：param/dose 分层 / split_contemp 入快照 / 对齐报告

## 一、SignalBundle 拆成三层

以前 `bundle.beta` 对分布滞后边存的是**剂量**（含 z，随日变），对其他边存的是**参数**（常数）。
做时序分析（如"β 的稳定性"）会把两类东西混在一起。

现在显式三层：

| 字段 | 含义 | 是否随日期变 |
|---|---|---|
| `param` | 纯参数：DML 的 β、DL 的累积 β、事件的 CAAR/窗口 | 否（DML 随状态变） |
| `dose` | 参数 × 读数：β×z、Σβ_k z_{t−k}、CAAR/窗口 | 是 |
| `contrib` | magnitude × dose × gate × active × 纯度 × 释放比例 | 是 |

`beta` 保留为 `param` 的别名（兼容旧调用）。验证：DL 边的 `param` 恒定、`dose` 逐日变化。

## 二、split_contemp 写入快照 + 一致性校验

`split_contemp` 决定"lag0 进不进 α"，但以前只存在 config 里。改了配置而没重估，
就会出现"path 是旧的（含 lag0）、规则是新的（跳过 lag0）"。

现在 `EdgeModel.split_contemp` 在 `fit_all` 里定下、写入快照；`_decode` 校验：
不一致就报错并提示重跑 `cli edges`。

## 三、AlignReport 口径修正

```python
# 旧：拿原始索引的非空行数减日历索引的非空行数（两个索引不同，差值是假的）
report.filled = after - before
report.extra  = out.isna().all(axis=1).sum()   # 混了"早于起始日"和"整列全 NaN"
```

改为在**同一个日历**上比较"原生有值"与"填充后有值"：

- `filled` = 只靠 ffill 才有的交易日数
- `extra` = 早于首个观测、无法填充的交易日数

新增 `tests/test_calendar_align.py`（4 条），覆盖有值间隔、前置空档、全空输入、DataFrame。

## 四、其它

- `_fit_dml_ols` 的基准组只从"数据里出现过"的标签里选
- 卡片第 2 节加了"t 日读数 → t+1 日收益"的说明
- 测试 15/15

## 未做

- 黄金没有 `LOCAL_NODES`，非共享节点一律 fallback 到 "D"（当前各边显式写了 freq，未触发问题）。
  等黄金要按周频窗口标准化时再补。

---

# v5.11（2026-10-26）：split_contemp 的 lag0 混进 α / 频率绑定到快照

## 一、`dl_aggregate` 没跳过 lag0（真 bug）

`EdgeModel.path` 一定含 `k=0`（同期项），但 `dl_aggregate` 把它一起加了进去，
违反 `edge.py` 里"lag0 只诊断、不进 α"的声明。

影响（A 股 INSIDER_NET，当日）：

```
旧 agg（含 lag0）= +0.157
新 agg（只 lag1+）= -0.190
lag0 项本身    = +0.347
```

不只是数值变化，**符号都翻了**。修复后 α(当日) 从 −0.08 变为 −0.22。

修法：

```python
if edge.split_contemp and int(k) == 0:
    continue
```

## 二、频率绑定到快照（`freq_used`）

`resolved_freq()` 原来只查 `SHARED`，不看 `LOCAL_NODES`；信号层也没有"当时用的频率"，
配置一改就会按新频率算 z 去配旧 β。

改为：`EdgeModel.freq_used` 在 `fit_all` 里由 `e.resolved_freq(local_nodes)` 定下并写入快照，
信号层一律用 `freq_used`，缺失才回退到当前配置。

## 三、小样本 OLS 的基准组

`base_label` 原来只判 "不在独热列里"，可能取到**从未出现**的标签。
改为"在数据里出现过、且不在独热列里"。A 股不触发，黄金的月频 DML 边会走到。

## 四、卡片说明

第 2 节加一行："本表是 t 日读数对 t+1 日收益的预期；贡献 ≈ 权重 × 敏感度 × 读数 × 纯度"。

## 未采纳 / 待办

- `SignalBundle.beta` 的语义混用（DL 存 dose、其它存 param）→ 建议和"param/dose/contrib 三层"
  一起做，属重构，未动。
- `split_contemp` 未写入快照（仍从当前 Edge 取）→ 建议随下次 schema 升版一起加校验。
- `AlignReport.filled/extra` 口径 → A 股不走 `merge_shared_nodes`，暂缓。
- `magnitude_by_label` → 不采纳。`magnitude` 是先验权重、`direction_by_label` 已表达符号异质性；
  再加一层按状态变化的强度先验会让模型不可反驳。

---

# v5.10（2026-10-26）：z 统一 / 事件 gate 方向 / 频率解析

四个互相纠缠的问题，根都是"同一件事有多份实现"。

## 一、DML 贡献漏乘 z（最严重）

```python
base = e.magnitude * b * gate * active * sig      # 旧：只有 β
```

后果：**+2σ 和 +0.2σ 的贡献完全一样**，α 退化成"状态平均信号"。

按估计器分流（不能一律乘，会重复计算）：

| 估计器 | driver |
|---|---|
| DML | `β × z` |
| 分布滞后 | `agg`（z 已在 Σβ_k z_{t−k} 里） |
| 事件研究 | `caar / window`（gate 负责触发） |

## 二、事件 gate 用 `abs(z)`

估计层是单边的（`event_sign=+1` → `z >= 3`），信号层用 `z.abs()`，缩量日会错误激活。
已按 `event_sign` 分流。A 股成交量 z 从未跌到 −3，历史上 0 天错误激活（休眠 bug）。

## 三、z 有四种算法 → 统一到 `zscore.py`

| 位置 | 旧算法 |
|---|---|
| DML 估计 | 全样本均值/标准差（实盘不可得 = 前视） |
| 分布滞后估计 | 边频率上的全样本 |
| `dl_aggregate` | 边频率滚动 24 期 |
| 卡片 z 列 | 日频滚动 252 期 |

统一为**边自己频率上的滚动 z**，估计层与信号层共用 `periodic_z` / `edge_z`。
窗口按频率取（D=252, W=104, M=24, Q=8）。

## 四、所有边的 freq 都是 `"ME"`

`Edge.freq` 默认 `"ME"`，连日频节点（VIX、AMOUNT_SHOCK）也被当月频，
信号层把它们**重采样到月度**再算 z —— 卡片上的"日频 z"实际是月频值。

改为 `freq=None` + `Edge.resolved_freq()`：未指定则跟随源节点。

## 验证

```
事件边：信号层触发 34 天 == 估计层事件 34 个     ✓
分布滞后：卡片 z 与估计用 z 逐位一致             ✓
VIX 频率解析：D（不再被当月频）                   ✓
测试 11/11
```

## 影响

α 整体变小、更接近 0——读数平静时 z≈0，贡献就该是 0。
以前 β 主导，读数再小也有贡献。这是修正，不是退化。

---

# v5.9（2026-10-26）：极端两层 / prep 统一 / 标签外推 / README

## 一、修正：10-23 出不来卡片，不是社融的锅

`features` 完备到 10-23，真正原因是 **`labels.csv` 停在上次 fit 日**，
而 `daily` 只读它、不会外推。

修法：`build_panel` 发现断档期就用 `hmm_last.pkl` 逐日预测补标签，极端日走规则。
因果上干净——模型固定在过去，只用新数据预测。

社融接口确实坏了（列名"存量"→"增量"、月份 `YYYYMM`、数据只到 2026-04），
但它不在 HMM 特征里，只影响 `EQUITY_SUPPLY` 的控制变量。

## 二、prep_features：唯一实现

```
fit_regime / walk_forward / 在线后验 → 全部调 regime_engine.prep.prep_features
顺序固定：先按原始分位 winsorize，再标准化
```

新增 `tests/test_regime_prep.py`：断言在线重算的后验与 `last_proba` 逐位一致。

## 三、极端行情：HMM 管常规，规则管极端

`regime_engine/extreme.py`：

| 项 | 规则 |
|---|---|
| 进入 | `\|ret\| > 3σ`（滚动 60 日）或 `realized_vol > 99 分位`（滚动 252）或 `\|ret\| > abs_cap` |
| 标签 | `extreme_up` / `extreme_down` / `extreme_vol`（**不合并**） |
| 退出 | 连续 3 日不触发 |
| 持续期 | 几何分布近似 |

- HMM 只在非极端日训练与解码
- 常规词表 6 → 4：`range / trend_up / trend_down / distribution`
- `regime_valid` 不含极端标签 → 极端日 α 显示"不适用"
- 参数在资产 config（`EXTREME_PARAMS`）

结果：A 股 K=3（3 常规 + 3 极端），黄金 K=4（4 + 3）。

## 四、命名策略修正

- 池子顺序 = 标签偏好顺序，不再按 proto 重排
- neutral 组按波动**从低到高**配标签：低波=震荡、高波=派发

## 五、其它

- `LABEL_NAME` 合并极端标签中文名
- `edge.validate` 不再校验 target（上游边 target 不是主标的）
- DML 跳过全 NaN 控制变量、丢弃零散 NaN
- README 重写
- 测试 11/11

---

# v5.8（2026-10-24）：regime 包代码审查

## 一、修 bug：A 股的"波动"维度是死的

`label.VOL_COL = "realized_vol_5d"`，但 A 股特征叫 `realized_vol_20`。
`name_states` 里这行于是静默退化成 `v = r * 0`：

```python
v = means[VOL_COL] if VOL_COL in means.columns else r * 0
```

后果：组内"按波动排序"变成按状态编号排序，`panic`/`rebound` 这两个
**靠波动区分**的标签实际是随机分配的。这也解释了之前 `panic` 的平均收益是正的。

修法：`regime_engine.align.resolve_vol_col()` 自动识别（优先 VOL_COL，
其次任何 `realized_vol*`，都没有才报 warning），两个资产共用同一套命名实现。

## 二、命名阈值：硬编码符号 → 按收益排序切三段

```python
# 旧：r < -2e-4 → bear；r > 2e-4 → bull
# 新：按收益排序，前 1/3 bear、后 1/3 bull、中间 neut
```

- K≥3 时三组都非空（`three_way_groups` 带兜底，保证给 neut 留位置）
- K=2 只分低/高两组，不硬造 neut
- K=1 全部归 middle

**副作用（需要知道）**：当全样本所有状态收益都是正的（黄金就是），
排序切段会**强行把收益最低的状态命名为"恐慌"**。当前黄金 `stress` 的平均收益
是 **+0.029%**——是"最不涨"而不是"下跌"。这是排序法的固有代价。

## 三、未来状态预览（黏性 / 切换预警）

新增 `causual_core/regime_online.py`：

```
今日后验 p_t（最后一个 walk-forward 窗口的模型现算）
p_next = p_t @ 转移矩阵
→ 留在当前状态的概率、最可能转向、期望停留天数（1/(1-p_ii)）
```

### 模型与快照分开存

- `spec`（`data/regime/{asset}/current.json`）= **历史快照**，只用于复现/审计
- `saved_models/{asset}/hmm_last.pkl` = **推理用的最后窗口模型**
- 两者用 `fit_id` 对齐；对不上就跳过状态预览并告警

之前把 `last_window` 塞进 spec 是错的：两者的生命周期不同，混在一起必然漂移。

### 修 bug：clip 与 standardize 的顺序反了

`_prep` 里原来先标准化再按**原始分位**裁剪，等于用原始量纲的阈值去裁 z 值，
几乎所有点都被压到边界，后验完全失真——表现为"边际后验 100% 指向派发，
但 Viterbi 判修复"。改成和 `walk_forward` 一致（**先裁剪再标准化**）后，
重算的后验与 fit 时存储的 `last_proba` 逐位一致。

**之前记的"Viterbi 与后验会打架"不成立**，那是这个 bug 的表现。
`current()` 现在直接用后验 argmax，和 labels.csv 同口径。

## 四、两类遗留问题（未修）

1. **全正样本被强行分配 bear 标签**（黄金 `stress` +0.029%）。
2. **K 小于词表时，选中哪些标签是任意的**：A 股 K=4 拿到的是
   `panic/rebound/range/distribution`，`trend_up/trend_down` 一次都没出现。
   而"反弹/恐慌"更像事件而非状态，K=4 用 `range/trend_up/trend_down/panic` 可能更合理。

---

# v5.7（2026-10-22）：时间窗一致性 / 标签对齐 / 国家队事件改口径

## 一、Card A 展示时间轴

分布滞后边不再只显示累积效应，而拆成**短期(lag0) ｜ 长期(lag1+)**

```
内部人净增持 | 短期 -2.2% ｜ 长期 +2.2% | ⚡ 短期反向，中长期正向（滞后发酵）
```

一致性判定改成按时间窗：

| 情形 | 标记 |
|---|---|
| lag1+ 与理论同向、lag0 反向 | **⚡ 短期反向，中长期正向（滞后发酵）** |
| lag1+ 与理论反向 | **⚠ 逻辑冲突（中长期反向）** |
| 纯度不足 | ⚠️ 机制不显著 |

以前 lag0 反向就直接判"方向冲突"，把"跌了才增持"这种正常现象误报成了逻辑错误。

## 二、HMM 标签对齐（不是全样本预测）

用户转述的建议是"全样本训练一次，再对历史每天 predict"。**这个会引入前视偏差**：
2020 年的标签会用上 2025 年的数据，回测业绩会被高估。

但问题本身是真的——逐窗命名会让同一状态在不同年份叫不同名字。

折中方案（`regime_engine/align.py`）：**用全样本模型的状态均值作参照，
每个窗口的状态先做匈牙利匹配再套参照标签**。估计仍是逐窗无前视，
只有"命名"用了全样本信息（和之前用全样本定词表是同一类取舍）。

标签抖动诊断（`label_churn`）：A 股四个标签各有 26~60 个不连续区段，
说明状态切换本身频繁，不全是命名问题。

## 三、国家队事件改为单日 95 分位

- 旧：份额 z>2 且连续两日 → 78 次，+0.052%/日
- 新：**单日份额变动 > 滚动 60 日 95 分位** → **157 次**，估计仍为负（方向冲突）

事件数翻倍后方向没回来。这条边目前不可用。

---

# v5.6（2026-10-22）：时间轴 + 时间序列交叉验证 + 节点传输标签

## 一、α 重新标定

`ALPHA_SCALE = 0.5`（原来 tanh 的分母是 1.0）。这是**展示尺度**，不改变排序。

## 二、时间轴：分布滞后不再揉成一个标量

新增 `HORIZON = 5`（A 股主观交易以一周为操作周期）。

每条边把"当下总效应"按**释放曲线**摊到未来 5 个交易日：

| 节点类型 | 释放曲线 |
|---|---|
| 快变量（`max_lag=0`：汇率、VIX、美元指数） | `[1, 0, 0, 0, 0]` —— 当日释放完 |
| 慢变量（增持、社融、股票供给） | `[0.43, 0.26, 0.16, 0.10, 0.06]` —— 指数衰减，半衰期 2 天 |

分布滞后边的总效应也改成**真正的滞后加权**：
`agg_t = Σ_k β_k · z_{t−k}`，在边自己的频率上算完再 ffill 回日频
（`signal.dl_aggregate`），而不是像以前那样只用当期 z 乘一个累积 β。

卡片同时输出两个口径：

```
α(当日) = -0.28 ｜ α(未来 5 日累积) = -0.43 ｜ 原始预测 -14bp / -23bp
```

两者的差就是"慢变量的余温"。

## 三、TimeSeriesSplit：修掉一个真实的前视偏差

`LinearDML` 原来用 `cv=5`（随机 KFold）。因为 `y` 是 `shift(-1)` 的未来收益，
随机分折会让训练集包含测试期之后的样本——**这是实打实的前视**。

改为 `cv=TimeSeriesSplit(n_splits=5)`。已验证 econml 接受 sklearn 的 splitter 对象。

**小样本回退**：`n < 300` 时不做 DML（交叉验证后每折训练集不足百条），
退化为 `T + 标签独热 + T×独热 + 控制变量` 的 OLS（HAC 标准误，见 `_fit_dml_ols`）。

## 四、节点传输标签 → 估计器自动选择

`Node` 增加两个字段：

```python
transmission: Literal["fast", "slow"]   # slow → 分布滞后；fast → 当期 DML
max_lag: int
```

- 全局池（`nodes.SHARED`）标好了共享节点
- 资产专用节点在各自 `config.LOCAL_NODES` 里声明
- `Edge.estimator` 改为**可选覆盖**：`None` 时按节点标签自动决定

实测（A 股）：

```
USDCNY      override=None -> causal_forest   span=0
INSIDER_NET override=None -> distributed_lag span=3
```

## 五、结果

| | 数值 |
|---|---|
| α(当日) | −0.28 |
| α(5日累积) | −0.43 |
| 原始预测 | −14bp / −23bp |

慢变量（内部人增持）在 5 日口径里贡献更多，符合"余温"的预期。

---

# v5.5（2026-10-22）：纯度阈值放宽 / dispersion 固定口径 / 分段验证

## 一、方向纯度门槛降到 0.5

`signal.PURITY_MIN = 0.5`（一个常数，改它即可全局生效）。

换算关系：

| 纯度 | 单边置信 | 双侧 p |
|---|---|---|
| 0.50 | 75.0% | 0.50 |
| 0.70 | 86.5% | 0.26 |
| 0.90 | 95.0% | 0.10 |

0.5 是**很宽松**的门槛（等价于"符号对就行"），效果是多数边不再被打折。
选它是用户的判断（"75% 的把握可以接受"），不是统计惯例。

## 二、ETF dispersion 固定三组

原来用 上证50+沪深300+中证1000+中证2000 四组，但中证2000 ETF 2023-09 才有，
导致 2016-2022 是三组、2023 后是四组——**特征定义在 2023 年断裂**。

改为固定三组（都有 2016 年起完整历史）。重拟后 **K 从 5 变 4**，
标签从 `range/trend_down/rebound/panic/distribution` 变为
`range/rebound/panic/distribution`——证明原来的断点确实影响了状态划分。

中证2000、科创板、红利改为**只观察、不进模型**（历史太短：2023-09 / 2025-03 / 2019-12）。

## 三、国家队事件收紧

阈值从 z>1.28（90 分位）改为 **z>2 且连续两日净申购**：

| | 旧 | 新 |
|---|---|---|
| 事件数 | 113 | **78** |
| 结果 | −0.082%/日（方向反） | **+0.052%/日**（方向正） |

收紧后方向修正了，但量级很小。

## 四、内部人增持：分段验证**不稳健**

把 119 个月四等分，各自重估（`output/ashare/insider_segments.md`）：

| 区间 | lag1-3 累积 | p | 同号 |
|---|---|---|---|
| 2018-04 ~ 2020-05 | **−0.41%** | 0.816 | 否 |
| 2020-06 ~ 2022-07 | +1.67% | 0.246 | 是 |
| 2022-07 ~ 2024-09 | **+3.41%** | 0.004 | 是 |
| 2024-09 ~ 2026-10 | +2.84% | 0.071 | 是 |

全样本的 +2.19% 主要由 2022 年之后贡献，**第一段是负的**。
所以那条"纯度 0.99"是**全样本的统计错觉**，不代表机制稳定。

## 五、详情卡新增"激活路径的贡献计算"

逐条列出 `权重 × β × z × gate × active × 纯度 = 贡献` 的实际数字，
合计与 `bundle.predicted` 对得上（本次 +1.8bp，α=+0.018）。

---

# v5.4（2026-10-22）：方向纯度 + ETF 份额入 HMM + 国家队事件

## 一、方向纯度替代"CI 是否含 0"

新的显著性度量：

```
SE = (CI_hi − CI_lo) / (2 × 1.96)
z  = β / SE
Purity = |2Φ(z) − 1|
```

含义是"β 为正的概率与为负的概率之差"，0.90 ≈ p<0.10，0.95 ≈ p<0.05。
比二分判断平滑，不会因为跨过临界值就跳变。

**用法**：贡献 ×= 纯度（连续权重），替换原来"不显著打 5 折"的硬切换。
卡片"一致性"列显示纯度，<0.90 标 `⚠️ 机制不显著（纯度 x.xx）`。

- DML 边用 `beta_ci[label]`，DL/事件用 `cumulative_ci`，事件研究用 t 反推 SE
- 没有 CI（样本不足）→ 纯度 0，贡献为 0（比原来"当不显著"更严格）

## 二、ETF 份额：因果边 → HMM 特征

`SIZE_FLOW → CSI_ALL` 删除。理由（用户）：份额差测的是**资金分歧度/避险偏好**，
不是涨跌方向，当因果边属于误用。

改为 HMM 特征 `etf_dispersion`：
```
std(各类指数份额变化率) / mean(|变化率|)
```
高 = 各风格申赎方向不一致（分歧、避险轮动）；低 = 同向。

特征池 8 → **9 维**，HMM 重拟：K=5，签名 `2c3358bf3f6e`，
标签变为 `range / trend_down / rebound / panic / distribution`。

## 三、国家队 ETF 事件

```
NATIONAL_ETF = Δ(510300 份额) + Δ(510050 份额)   （亿份）
```
事件：z > 1.28（约 90 分位）→ 事件研究，窗口 (−1, +5)，关联层。

**结果**：−0.082%/日（113 个事件），**方向与理论相反**、不显著。
即"份额骤增后 5 日偏弱"——可能是套利盘申赎而非国家队托底，
也可能事件定义太宽（1.28σ 抓到太多普通波动）。

## 四、USDCNY 的对照实验：结论出乎意料

用户假设："USDCNY 不控制 USD_INDEX 时会把美元指数的效果归因给汇率"。
实测**不支持**这个假设：

| 标签 | 控制 USD_INDEX | 不控制 |
|---|---|---|
| panic | +0.0306 | +0.0354 |
| range | −0.0906 | −0.0230 |
| rebound | −0.1822 | −0.1426 |
| trend_down | −0.0920 | −0.1061 |

两组差异很小，**控制之后汇率系数反而更负**（range 从 −0.023 变成 −0.091）。
说明 USDCNY 的弱效应不是"被美元指数抢走了"，而是它本身在 A 股定价里就弱。

`output/ashare/usdcny_contrast.md` 保存了完整对照。

---

# v5.3（2026-10-22）：月频增持 + 全量滞后路径 + 双卡片

## 一、内部人增持改月频

周频 → 月频，**132 个月**（2015-11 ~ 2026-10）。这份数据要手工维护，周频负担太重。

**月频结果与周频不同**（本次最重要的发现）：

| 滞后 | 系数 | 95% CI |
|---|---|---|
| lag0 | **−2.17%** | [−3.76, −0.59] |
| lag1 | +1.02% | [−0.92, +2.96] |
| lag2 | −0.05% | [−2.04, +1.93] |
| lag3 | +1.23% | [−0.33, +2.78] |
| **lag1-3 合计** | **+2.19%** | **p = 0.013** |

周频时 lag1-4 合计是 −0.29%（不显著），**月频变成 +2.19% 且显著**。

读法：
- lag0 显著为负 → 当月增持多的月份，当月指数反而跌（"跌了才增持"的反向因果）
- **lag1-3 显著为正** → 排除同期反向因果后，内部人净增持对**未来 3 个月**有正向预测力

这是目前 A 股唯一一条统计显著的边。注意它衡量的是**三个月的累积效应**，
不是次日信号——卡片里的"日当量"（÷21）只是为了让不同频率的边能加总。

## 二、所有分布滞后边输出 lag0~3 路径

`fit_distributed_lag` 不再只在 `split_contemp` 时计算路径：**所有 DL 边**都输出
`path`（lag0..min(3,max_lag)）与逐期 95% CI，写进快照并在详情卡展示。

`split_contemp` 只决定 α 用哪一段（排除 lag0 还是包含）。

## 三、双卡片

| 文件 | 用途 | 内容 |
|---|---|---|
| `card_YYYY-MM-DD.md` | **交易指导** | 状态、净指向分解、α |
| `detail_YYYY-MM-DD.md` | **模型检查** | 状态画像、边清单、分状态 β/CI/p、DL 路径、数据新鲜度 |

详情卡明确标注"不构成交易建议"。

## 四、SIZE_FLOW 接上（周频）

- ETF 份额按**周频**采集（552 个周五），6 线程并发，约 12 分钟
- 覆盖：上证50 / 沪深300 / 中证1000 / 中证2000 / 红利 / **科创板**
- `SIZE_FLOW = mean(中证2000, 中证1000 份额变化率) − mean(上证50, 沪深300 份额变化率)`
- 边：`SIZE_FLOW → CSI_ALL`，freq="W-FRI"，max_lag=3，关联层，n=461

**结果**：lag0 +0.08、lag1 +0.11、lag2 −0.04、lag3 +0.04，累积 +0.19%（p=0.255）——不显著。

## 五、深交所 ETF 无历史（PROBLEM P28）

名单里的 159xxx（创业板、部分 300/1000/2000）都在深交所，
`fund_etf_scale_sse` 拿不到；`fund_etf_scale_szse` 只返回当天快照、无历史。
创业板因此换成科创板（589xxx 全部在上交所）。

## 六、其他修正

- `contributions_at` 的显著性判断补上"DL 边只有累积 CI"的兜底，
  否则内部人增持明明 p=0.013 却会被标成"机制不显著"、贡献被打 5 折。
- 详情卡里非 DL 边的 freq/max_lag 显示为"—"。

---

# v5.2（2026-10-21）：内部人增持、CI 打折、ETF 份额采集

## 一、新增：内部人净增持边（周频）

数据：`data/manual/市场概况_数据中心_东方财富网_{股东,高管}增持.xlsx`
（日度，含增持/减持家数、金额、净额，单位是"亿/万"字符串）

处理：
- 解析"2.49亿""528.96万"这类中文单位 → 元
- 股东 + 高管合并为**净增持金额**（亿元），**按周汇总**（W-FRI）降低日频噪音
- 570 周，2015-11 ~ 2026-10

**结果（split_contemp，lag0..4 周）**：

| 滞后 | 系数 | 95% CI |
|---|---|---|
| lag0 | **−0.41%** | [−0.82, −0.00] |
| lag1 | −0.14% | [−0.48, +0.20] |
| lag2 | +0.23% | [−0.10, +0.56] |
| lag3 | −0.04% | [−0.29, +0.22] |
| lag4 | +0.07% | [−0.24, +0.37] |
| **lag1-4 合计** | **−0.29%** | 不显著 |

读法：**同期负相关**说明内部人是在跌了之后才增持（反向因果，符合"护盘"直觉）；
滞后 1-4 周合计为负但不显著 → "增持后股价走强"这个假设在数据上**不支持**。

## 二、信号层：CI 跨 0 打 5 折

`signal.py` 新增 `_ci_significant()`：某条边在当前标签下的 95% CI 若跨 0，
贡献乘 0.5；卡片对应行的"一致性"列显示 **⚠️ 机制不显著**。

效果（2026-10-21 A 股卡片）：Shibor 边的贡献从 −34bp 降到 −17bp，
α 从 −0.33 变为 −0.17。**避免"读数极端"被误当成"机制可靠"。**

## 三、删除：PMI 边

符号与理论相反（−0.17%）、n=159、不显著。按用户意见移除，
未来只作为混杂/效应修饰（见 PROBLEM P25）。

## 四、ETF 份额采集（后台）

`causual_assets/ashare/etf_shares.py`：
- 覆盖 6 类指数、多只 ETF（上证50/沪深300/中证500/中证1000/红利）
- **用份额变化率而不是绝对份额差**，避免不同 ETF 规模量级差异
- 6 线程并发 + 每 400 行落盘 + 断点续跑，实测约 2.2 天/秒，全历史约 20 分钟
- 输出 `data/cache/etf_shares_sse.csv`，后续只做增量

`style_flow()` 把各类指数的份额变化率取均值后做**小盘 − 大盘**差。
面板里的 `SIZE_FLOW` 在采集覆盖 ≥500 天时自动启用，否则置 NaN。

## 五、单位修正

分布滞后的日当量按频率换算：月 `÷21`、季 `÷63`、**周 `÷5`**（原来一律 ÷21）。

---

# v5.1（2026-10-21）：A 股全面重做（中证全指 + 量价状态 + 股票供给）

## 一、目标与结构

标的从沪深300 改为**中证全指 000985**（腾讯源，2012-2026）。

**设计原则**：只把相对外生的变量当因果边；成交量/换手/两融/风格比价这些
内生于价格的，只做状态与修饰。

## 二、HMM 特征（8 维）

`log_return`、`realized_vol_20`、`volume_z`、`up_day_ratio`、`size_mom`（1000/300 比价动量）、
`margin_chg_z`、`rv_ratio`、`shibor_chg_z`

原计划的 `limit_ratio`（涨停−跌停）因涨停池无历史而移除，见 PROBLEM P20。

结果：K=5，标签 `panic/range/rebound/trend_down/trend_up`，Bhattacharyya 1.096。

## 三、9 条边

| 边 | 类型 | 时滞 | 估计结果 |
|---|---|---|---|
| REAL_YIELD → 全指 | DML | 1-2 日 | 各状态接近 0 |
| USDCNY → 全指 | DML | 当日 | −1bp/σ（极弱） |
| USD_INDEX → 全指 | DML | 1-2 日 | 恐慌/反弹为负、趋势下行为正 |
| VIX → 全指 | DML | 当日-隔夜 | **全状态为负**（−12 ~ −30bp），恐慌态最强 |
| SHIBOR3M → 全指 | DML | 1-2 日 | 多数为负，恐慌态转正 |
| PMI → 全指 | 分布滞后 | 1 月 | −0.17%（符号与理论相反，n=159） |
| MARGIN_BALANCE → 全指 | DML（关联） | 1-2 日 | 震荡/上行/反弹为负，**下行/恐慌为正**（反向） |
| EQUITY_SUPPLY → 全指 | 分布滞后 + split_contemp | 1-6 月 | **无数据，跳过** |
| AMOUNT_SHOCK → 全指 | 事件研究 | 1-5 日 | +0.87%（28 个事件，放量后偏多） |

## 四、估计器改造：split_contemp

按用户方案实现：
- `Edge.split_contemp` 开关（当前只有 EQUITY_SUPPLY 打开）
- 输出完整滞后路径 `path`（lag0..6）+ 逐期 CI
- `beta_lagged` = lag1..6 合计，**进 α 只用这一段**
- lag0 单独记作"同期相关（含反向因果）"，卡片不称其为"效应"

由于 EQUITY_SUPPLY 缺数据，这段逻辑尚未在真实数据上跑过（有单元行为，无实证）。

## 五、数据层现实（详见 PROBLEM P20-P24）

| 项目 | 结论 |
|---|---|
| 中证全指 | ✅ 腾讯源，无成交额 → 用 `volume_z` 代替 |
| 涨停/跌停池 | ❌ 只有最近 1 个月 |
| ETF 份额 | ❌ 单次 7.8 秒，全历史 5 小时+ |
| Shibor 3M | ✅ 替代 DR007 |
| 两融 | ✅ 分年拉取，2016 起完整 |
| 社融 / 净融资额 | ❌ 接口变更 / 无接口 |

## 六、结果

`output/ashare/cards/card_2026-10-21.md`：当前状态 **恐慌杀跌**，α = −0.33。

8/8 测试通过；黄金卡片不受影响（`output/gold/cards/card_2026-10-19.md`）。

---

# v5.0（2026-10-21）：三模块重构 + A 股插件

## 汇总

| 类别 | 条目 |
|---|---|
| 新增 | `regime_engine/`（顶级包）、`causual/causual_core/`、`causual/causual_assets/{gold,ashare}/`、`tests/` |
| 修改 | 快照按资产隔离、`daily --asset`、状态枚举下沉到各资产、estimators 改为外部传标签 |
| 删除 | 无（旧模块受沙箱限制无法删除，见 PROBLEM P15） |

## 一、目录

```
regime_engine/                 独立顶级包，不含任何资产知识
├── api.py                     fit_regime(features_df, k, seed) —— 唯一入口
├── hmm.py                     训练 / Viterbi / 平滑
├── select.py                  K 值选择（BIC + 2% 容忍带）
├── bhattacharyya.py           状态分离验证
└── serialize.py               参数字典 ↔ saved_models/{asset}/hmm.pkl

causual/
├── causual_core/              与资产无关的因果内核
│   ├── nodes.py               全局节点池（含"自然日/资产日历"标记）
│   ├── edge.py                Edge 数据类 + validate()
│   ├── calendar.py            align_to_trading_calendar / merge_shared_nodes
│   ├── estimators.py          DML / 分布滞后 / 事件研究
│   ├── signal.py narrative.py snapshot.py evaluate.py
│   ├── daily.py               python -m causual.causual_core.daily --asset gold
│   └── cli.py                 regime / edges / audit
└── causual_assets/
    ├── gold/       config(11 条边) fetch label pipeline
    └── ashare/     config(12 条边) fetch label pipeline
```

## 二、状态枚举下沉到资产

`RegimeLabel` 不再在核心里。黄金 6 个（range/trend_up/trend_down/recovery/stress/distribution），
A 股 6 个（range/trend_up/trend_down/rebound/panic/distribution）——**两套独立枚举**，
`regime_valid` 用各自的值，跨资产的标签拼错会在 `edge.validate()` 时直接报错。

`regime_engine.fit_regime` 不知道标签含义：它返回状态编号 + 原始量纲的状态均值，
标签由资产的 `label.name_states()` 决定。逐日解码时把 labeler 作为回调传进去。

## 三、estimators 的接口变化

旧版从全局 `paths.EDGES` 取标签做独热；现在**调用方传入 `labels` 列表**：

```python
fit_all(df, edges, labels=list(LABELS), treatment_col=..., target=...)
```

同一个 `fit_dml_edge` 因此可以同时服务黄金和 A 股，β 各自独立（快照也各自保存）。

## 四、按资产隔离的产物

| 产物 | 路径 |
|---|---|
| 状态模型 | `data/regime/{asset}/current.json` + `labels.csv` |
| HMM 参数 | `saved_models/{asset}/hmm.pkl` |
| 边模型快照 | `data/snapshots/{asset}/edges_YYYYMMDD.json` |
| 卡片 | `output/{asset}/cards/card_YYYY-MM-DD.md` |

## 五、HMM 序列化

按方案 B：`.pkl` 里存的是**纯参数字典**（means/covars/transmat/startprob +
scaler mean/scale + winsorize 边界），加载时重建 `GaussianHMM`。
跨 hmmlearn 版本不会失效。

## 六、日期对齐（验收 1）

`calendar.align_to_trading_calendar` 只做一件事：`reindex(资产日历, method="ffill")`，
**不会引入日历之外的日期**。资产的日历 = 该资产面板自身的索引。

测试造了两套日历（黄金含春节休市、A 股含国庆休市），断言：
- 对齐后索引与资产日历完全一致；
- A 股交易日不出现在黄金面板，反之亦然；
- 缺口用前一个有效值 ffill，不留 NaN。

8/8 通过（`python tests/run_tests.py`，沙箱里装不了 pytest）。

## 七、延迟异质性（验收 2）

同一条 `FED_RATE -> 标的`：

| | 黄金 | A 股 |
|---|---|---|
| latency | `1-3 月` | `1-2 日` |
| 机制 | 实际利率/机会成本 | 外资风险偏好 + 国内流动性 |
| 标签枚举 | 黄金那套 | A 股那套 |

测试断言两者 latency 不同、机制不同、枚举不同、且不共享 Edge 实例。

## 八、A 股 MVP 结果

- 面板：沪深300 + OHLCV + 换手 + LPR/PMI + 共享宏观节点，2016-01 ~ 2026-10，2621 行
- 状态：K=4，标签 `panic/range/rebound/trend_down`，Bhattacharyya 1.348
- 卡片：`output/ashare/cards/card_2026-10-21.md`，α=+0.01
- **社融抓取失败**（DateParseError），对应边自动跳过（见 PROBLEM P16）

---

# v4.4（2026-10-20）：央行数据彻底改手工，去掉 IMF 联网

## 汇总

| 类别 | 条目 |
|---|---|
| 新增 | 4（WGC Q1 年报接入、87 国篮子、`build_cb_manual.py`、`doc/PROBLEM.md`） |
| 修改 | 3（`cb_gold_monthly.csv` 改三列格式、`flowdata` 只做读取、年度缩放） |
| 删除 | 2（IMF 联网抓取移入 `causual/trash/`、诊断脚本归档） |
| 修复 | 1（`CB_HOLDINGS` 的 −2355 吨假清仓，见 PROBLEM P3） |

## 一、央行数据的最终形态

`data/manual/cb_gold_monthly.csv` 现在是**唯一数据源**，三列：

```csv
date,buy_t,holdings_t
2016-03-31,,28257.2
2016-04-30,25.3,
...
```

- `buy_t`：当月净购金（吨/月）。2016-04~2023-12 = IMF 数据按年缩放；2024-01 起 = 用户读图。
- `holdings_t`：央行总持有量（吨）。**只在每年 3 月末填一次**（WGC Q1 快照），中间月份由脚本按锚点累加。

运行时只读这个文件，**不再有任何联网代码**。

## 二、87 国篮子与年度缩放

WGC 的 11 份 Q1 年报里，每年有 5~35 个国家的数据是空的（报送延迟），
且缺失不是均匀的——2016 年 118 国有数、2026 年只有 88 国，但总量反而从 28,979 涨到 32,016。
直接求和会把"某国补数"当成"某国增持"。

**固定篮子**：取 11 年都有数据的 87 国，覆盖 99.9% 的持有量。

**年度缩放**：每个"4 月→次年 3 月"窗口内，IMF 月度流量整体乘一个系数，
使累计等于两个 Q1 锚点的差额：

| 窗口 | 锚点差额 | 缩放系数 |
|---|---|---|
| 2016-04~2017-03 | +299 | 1.06 |
| 2017-04~2018-03 | +362 | 0.88 |
| 2018-04~2019-03 | +597 | 1.54 |
| 2019-04~2020-03 | +547 | 1.20 |
| 2020-04~2021-03 | +240 | 1.45 |
| 2021-04~2022-03 | +273 | **2.87** |
| 2022-04~2023-03 | +497 | 1.04 |
| 2023-04~2024-03 | +380 | 1.44 |

系数在 0.88~2.87 之间，说明 IMF 的 14 国口径与全球口径差距很大且不稳定——
这本身就证明"IMF 不能直接当全球购金用"。

## 三、修复：−2355 吨的假清仓

见 `doc/PROBLEM.md` P3。核心是"跨主体求和必须先固定主体集合"。

修复后单月跳变 >300 吨的次数从 3 次降到 **0**。

## 四、trash 归档

`causual/trash/`：IMF 抓取代码、两个诊断脚本、README（说明为什么废弃、恢复条件）。
生产路径不 import 它们（只有 `build_cb_manual --refresh-imf` 这个可选入口会用）。

## 五、重估结果

| 边 | 累积效应 | p | n | 变化 |
|---|---|---|---|---|
| `CB_GOLD_BUY` | **+0.842%/月** | 0.137 | 109 | 上版 +0.813（坏存量）→ 现 +0.842（真存量） |
| `ETF_FLOW` | −0.333%/月 | 0.440 | 109 | 不变（关联层，不进 α） |

---

# v4.3（2026-10-20）：央行购金接入手工月度数据 + 存量做控制变量

## 汇总

| 类别 | 条目 |
|---|---|
| 新增 | 3（`cb_gold_monthly.csv` 手工文件、`CB_HOLDINGS` 存量列、分布滞后支持控制变量） |
| 修改 | 3（CB 的 confounders 加入存量、ETF 改为**关联层**、月频读数显示方式） |
| 结果 | CB 累积效应 +0.62 → **+0.81%/月**，p 0.33 → 0.20 |

## 一、手工数据

`data/manual/cb_gold_monthly.csv`：2024-01 ~ 2026-08，共 32 个月，单位吨/月。
来源是用户从 WGC 月度图表目测读数（bar 上无精确数字，误差约 ±2 吨）。

合并规则（`flowdata.build_cb`）：

| 字段 | 2024 之前 | 2024-01 ~ 2025-06 | 2025-07 之后 |
|---|---|---|---|
| `flow` | IMF 逐国差分 | **手工数据** | 手工数据 |
| `holdings` | IMF 官方合计 | IMF 官方合计 | **官方值 + 手工流量累加** |

即：流量用手工（更新到最新），存量用官方（真实水平），官方结束后按流量外推。

## 二、为什么存量做控制变量（W），而不是效应修饰（X）或合成指标

| 方案 | 判断 |
|---|---|
| **存量做 W（采用）** | 存量决定"还有多少增持空间"，也是规模因子——同样买 1 吨，对 3000 吨和 100 吨的央行含义不同。放进控制变量后，流量的系数才干净 |
| 存量做 X（效应修饰） | 样本只有 109 个月，已经有 6 阶滞后 + 3 个控制变量；再加交互项会把自由度吃光，估计只会更不稳 |
| 合成一个指标（流量/存量） | 会丢掉"净购金多少吨"的直接解释，而且卡片上无法还原成可读的量；另外两者的数据口径不同（见下），比值会放大口径误差 |

卡片上的读数仍是**净购金吨数**（可读），存量只作为回归里的控制项。

## 三、口径问题（需要用户确认）

手工数据是**全球**央行净购金，而 IMF 合计只含 14 个主要购金国（约 5,800 吨）。
因此 2024-01 ~ 2025-06 这段里：

- `flow` 是全球口径（月度 20~65 吨）
- `holdings` 是 14 国口径（约 5,400 吨）

两者不能相互印证（`holdings` 的变化 ≠ `flow`）。当前只把存量当控制变量用，
影响有限，但如果要做"流量/存量"这类比值指标，必须先统一口径。

## 四、结果

| 边 | 累积效应 | p | n |
|---|---|---|---|
| `CB_GOLD_BUY`（+存量控制） | **+0.813%/月** | 0.20 | 109 |
| `ETF_FLOW`（改关联层） | −0.333%/月 | 0.44 | 109 |

CB 加入存量控制后系数从 +0.62 升到 +0.81，p 从 0.33 降到 0.20——方向一致，
说明"存量大的时候同样的购金更有意义"这个直觉是对的，但**仍未达到显著**。

## 五、ETF 改为关联层

按用户意见，`ETF_FLOW` 的 `layer` 从 `causal` 改为 `associative`：
v4.1 已证明资金跟随价格（相关 +0.54），因果链方向不成立，只能算同期条件关联。
它仍然 `use_in_alpha=False`。

## 六、月频读数的显示

月频序列不会每天变，原来显示"—（09-30 未更新）"会被误读成数据故障。
现在显示为 `+39.0吨（可见至 2026-09）`。

---

# v4.2（2026-10-20）：央行购金与 ETF 改月度，三项季频边停用

## 汇总

| 类别 | 条目 |
|---|---|
| 新增 | 2（IMF 央行月度储备、WGC ETF 月报解析 → `causual/flowdata.py`） |
| 修改 | 2（`CB_GOLD_BUY`、`ETF_FLOW` 从季频水平值改为**月度流量**） |
| 删除 | 3 条边（`JEWELRY_DEMAND`、`TECH_DEMAND`、`MINE_SUPPLY`，注释停用） |

边数：14 → **11**（上游 3 + 直接 8）。

## 一、央行购金：改用 IMF 逐国月度持仓差分

来源：IMF IFS via DBnomics（`M.<ISO>.RAFAGOLDV_OZT`，单位百万金衡盎司）。
覆盖 14 个主要购金央行（CN/RU/IN/TR/PL/KZ/UZ/SG/CZ/QA/TH/PH/IQ/SA），
先**逐国差分**再求和——这样某国新增或停报不会造成虚假的"购金"。

- 样本：1979-12 ~ **2025-06**，392 个月；2016 年后约 109 个月
- 单位换算：×31.1035 → 吨
- 只保留覆盖率 ≥70% 的月份（样本末尾个别国家延迟会造成合计骤降，看起来像净卖出）

**结果对比**：

| | 季度 level z（v4.1） | 月度流量（v4.2） |
|---|---|---|
| 累积效应 | +1.50%/季 | **+0.616%/月** |
| p 值 | 0.477 | 0.331 |
| n | 32 | **109** |

符号正确（+1），但仍不显著。改成流量之后不需要再去趋势——流量本身可正可负。

⚠ **数据只到 2025-06**（IMF 报送滞后约 16 个月）。这意味着卡片上的 CB 读数会
显示"未更新"、z≈0、贡献为 0。要拿到新鲜读数需要 WGC 月度央行购金报告
（用户可提供），落成 `data/manual/cb_gold_monthly.csv` 即可覆盖。

## 二、ETF 净流量：改用 WGC 月报持仓差分

来源：`data/manual/ETF_Flows_2026-10-02_1401.xlsx` 的 `Holdings by month` 页。
结构：第 0 行是基金代码、第 0 列第 6 行起是月末日期、第 2 列起是各基金持仓（吨）。
**只取表头非空的列**——前几列是汇总/元数据（表头为空），误加进去会得到 5740 亿吨的荒谬数字。

- 全球持仓：2003-03 ~ 2026-09，最新 **4,255 吨**
- 流量 = 逐基金差分后求和（吨/月）
- 2016 年后约 109 个月

**结果对比**：

| | 季度 level z（v4.1） | 月度流量（v4.2） |
|---|---|---|
| 累积效应 | −2.83%/季（p=0.003） | **+0.023%/月（p=0.973）** |
| n | 32 | **109** |

月度口径下**符号回到理论值（+1）**，但效应基本为零（CI ±1.3%/月）。
这与 v4.1 的发现一致：ETF 流入跟随金价（反向因果），不驱动金价。
因此保持 `use_in_alpha=False`。

## 三、珠宝 / 科技 / 矿产：整体停用

这三项只有 WGC 季频数据（~35 个季度），去趋势后全部不显著，
且合计只占黄金需求的小部分。按用户判断（"影响小、找不到更细频率"）整体注释停用，
与 COMEX 库存同样处理。恢复时需把三列加回 `REQUIRED_COLUMNS`。

## 四、连带改动

- `data.prepare` 对 `COMEX_INVENTORY` 做存在性判断——对应边停用后该列可能不存在，
  之前会直接 KeyError。
- `paths.PUBLICATION_LAG_DAYS`：`CB_GOLD_BUY` 45→30 天，`ETF_FLOW` 45→10 天。
- `REQUIRED_COLUMNS` 从 22 列降到 18 列。

---

# v4.1（2026-10-20）：去掉国际金价的因果边 + 修季频伪回归

## 汇总

| 类别 | 条目 |
|---|---|
| 删除 | 2 条边（`XAU_USD→GOLD_CNY`、`USD_INDEX→XAU_USD`） |
| 新增 | 3（`detrend` 去趋势、`use_in_alpha` 开关、归因里的国际金价参照线） |
| 修改 | 2（`USDCNY` 权重 0.65→0.80、季频边全部去趋势） |

## 一、为什么删掉 XAU 的两条边

国际金价与国内金价是**近似恒等式**：

```
GOLD_CNY = XAU_USD × USDCNY / 31.1035 × (1 + 境内溢价)
```

把它当因果边有三个问题：

1. 没有经济含义——等式右边含左边，谈不上"传导机制"；
2. 它的 β≈+0.6、权重 1.0，把其他所有机制都压住了（α 的方差几乎全由它贡献）；
3. 它让"归因"变成同义反复：金价涨，因为国际金价涨。

**XAU_USD 并没有被删掉**，而是降级为**混杂控制变量**：它留在
`USDCNY→GOLD`、`USD_INDEX→USDCNY`、`OPEN_INTEREST`、`COT_NET_LONG`、`ETF_FLOW`
的 `confounders` 里，用于阻断后门路径——美元同时驱动国际金价和人民币，
不控制它，汇率系数会被「美元计价效应」污染（v3 实测为负号）。

`USD_INDEX→XAU_USD` 一并删除：XAU 不再是 DAG 节点，它的上游边也就没有存在意义。

`USDCNY→GOLD` 的权重从 0.65 提到 0.80，成为汇率主通道。

## 二、两个异常系数的诊断

### ETF_FLOW 的负号：**反向因果**，不是"资金流出压制金价"

| 检验 | 结果 |
|---|---|
| corr(ETF 净流入_t, **上季度**金价收益) | **+0.536，t=4.22** |
| corr(ETF 净流入_t, 本季度金价收益) | −0.041 |
| 控制 0–4 期滞后后的逐期系数 | lag0 +0.001、lag1 −0.004、lag2 −0.004、lag3 −0.009、lag4 −0.008（**全部不显著**） |

结论：**资金在追涨**。金价先涨，下一季度 ETF 才流入；把流入当"原因"去解释金价，
等于把结果当原因。原来的 −2.83%/季（p=0.003）是水平归一化 + 反向因果的产物。

### MINE_SUPPLY 的正号：**趋势造成的伪回归**

矿产供给 2010→2025 从 2754 吨单调升到 3823 吨，它的 level z 几乎等于"时间序号"；
2016–2026 黄金季度收益均值为正，收益对时间序号回归必然得到显著正系数。

| 变量 | 水平 z | 去趋势后 |
|---|---|---|
| MINE_SUPPLY | +7.25%/季（p=0.002） | +5.66%/季（p=0.067） |
| CB_GOLD_BUY | +4.21%/季（p=0.000） | +1.50%/季（p=0.477） |
| ETF_FLOW | −2.83%/季（p=0.003） | +0.64%/季（p=0.681） |
| JEWELRY_DEMAND | +1.67%（p=0.380） | +1.32%（p=0.550） |
| TECH_DEMAND | −0.67%（p=0.523） | +1.44%（p=0.356） |

去趋势方法：处理变量改为「相对近两年（8 个季度）均值的偏离」，再标准化。
它对有正有负的流量序列也适用（同比百分比变化对负值无定义）。

结论：**央行购金的显著性是趋势给的**（p 从 0.000 掉到 0.477）；矿产的正号也主要来自趋势。
五个季频边去趋势后**没有一个显著**。

## 三、因此：季频边分两类

| 边 | 处置 | 理由 |
|---|---|---|
| `CB_GOLD_BUY` | 保留在 α（权重 0.55） | 去趋势后符号正确（+1）、量级 1.5%/季，虽不显著但方向一致 |
| `MINE_SUPPLY` ★ | 移出 α | 符号与理论相反、p 在 0.07~0.68 之间漂移 |
| `ETF_FLOW` ★ | 移出 α | 反向因果，控制滞后后无效应 |
| `JEWELRY_DEMAND` ★ | 移出 α | 同期互为因果（高价抑制需求），不可识别 |
| `TECH_DEMAND` ★ | 移出 α | 占比 7%，p≈0.36 |

新增 `Edge.use_in_alpha` 开关：标 ★ 的边仍留在 DAG 里做展示与符号审计，
但不进「预期次日」。卡片里它们带 `*` 标记，并有一行脚注说明。

## 四、归因里的国际金价参照线

因为 XAU 不再是因果机制，「今日归因」的残差会很大。为了让读者知道
**残差大是正常的**，加了一行参照：

```
| 参照：国际金价（不进 α） | +0.63×XAU | -0.14% | -9bp |
| 当日实际涨跌 | | | -8bp |
| 残差（未建模冲击） | | | -8bp |
```

对齐方式：COMEX 标注为 t−1 的那根日线在 t 日 05:00 收盘，正好被 t 日的上金所吸收。
实测 `shift(1)` 相关性 0.61（R²=0.37），`shift(0)` 只有 0.34，所以参照线用 `shift(1)`。

---

# v4（2026-10-19）：状态标签化 + 模块拆分 + 日常维护链路

## 汇总

| 类别 | 条目 |
|---|---|
| 新增 | 6（`regime/` 包、固定标签词表、快照持久化、`daily`、`regime fit/promote`、`edges fit/audit`） |
| 修改 | 6（`direction` → 按标签、`beta_series` 改为标签映射、卡片全状态表、签名校验、`causual.py` 不再拟合 HMM、合成数据用独立 spec） |
| 删除 | 2（`regime.py` 单文件、手工维护的状态编号依赖） |

## 一、为什么要做

三个问题，前两个是正确性、第三个是可用性：

1. **K=4 却只显示三个名字**：`state_roles` 把 K 个状态压成 `risk_on/transition/risk_off`，
   两个状态共用 `transition`，`regime_valid` 也一起被合并。
2. **状态编号会漂移**：重拟合后 S0/S1 可能互换，而 β 按编号存 → α 在重估边界无故跳变。
3. **每次出卡片要重跑 10 分钟**：没有快照，日常没法用。

## 二、新增

| 编号 | 项目 | 内容 | 为什么 |
|---|---|---|---|
| D1 | `causual/regime/` 包 | `labels` / `features` / `model` / `spec` / `compare` / `store` 六个模块 | HMM 是通用模块，不该塞在黄金流水线里 |
| D2 | **固定标签词表** | `RegimeLabel` 六个枚举值：range / trend_up / trend_down / recovery / stress / distribution | K 可变但词表固定；K 只决定用其中几个，下游 `regime_valid` 不用随 K 改 |
| D3 | 自动命名 | 按「收益符号分组 + 组内按波动排序」贴标签，允许同组合并 | 匈牙利一对一要求"熊市状态一定存在"，但 2016-2026 黄金几乎没有持续下跌态，会把正收益状态命名成"下行" |
| D4 | 快照持久化 | `data/snapshots/edges_YYYYMMDD.json`，只存 β/CI/累积效应/CAAR，不存模型对象 | 秒级加载、可 diff、不怕库版本升级 |
| D5 | `python -m causual.daily` | 校验 → 出卡片，**不拟合任何模型** | 日常入口 |
| D6 | `regime fit / diff / promote` + `edges fit / audit` | 拟合与生效分离，迁移表需人工确认 | 防止标签静默漂移 |

## 三、修改

| 编号 | 项目 | 原来 | 现在 |
|---|---|---|---|
| D7 | `Edge.direction` | 单个标量 | `direction_by_label` 字典（保留标量兜底），`dir_for(label)` |
| D8 | `beta_series` | 调 `cate.effect(X=mods)` | 按标签映射（β 只随标签变，快照可复现） |
| D9 | `_modifier_frame` | 硬编码 `transition/risk_on` 两个哑变量 | 按当前出现过的标签做 K−1 个哑变量 |
| D10 | 符号审计 | 全局一个符号 | 按标签逐行，冲突判定绑定"CI 排除 0" |
| D11 | 卡片第 1 节 | 只写"当前状态 + 原始编号" | 全状态画像表（标签/名称/占比/持续/收益/波动/当前）+ K 与标签数不一致时的说明 |
| D12 | 签名校验 | 无 | 快照签名 ≠ 当前标签签名时，卡片顶部出降级横幅，**停止输出净指向** |

## 四、删除

| 编号 | 项目 | 理由 |
|---|---|---|
| D13 | `causual/regime.py` | 拆到 `causual/regime/` 包 |
| D14 | `Panel` 里的 `state`（整数编号） | 下游一律用 `regime_label`；编号只存在于 HMM 内部 |

## 五、当前真实数据的状态模型

```
K=5，Bhattacharyya 1.043
标签签名 ea61dfbe7a86
range         42%  持续 8 日  +0.004%/日  波动 0.780%
trend_up      26%  持续 6 日  +0.122%/日  波动 0.609%
recovery      24%  持续 6 日  +0.108%/日  波动 1.206%
distribution   7%  持续 4 日  +0.051%/日  波动 0.881%
```

注意：**K=5 但只有 4 个标签**——有两个状态的收益/波动画像太接近，被合并。卡片会标注这一点。

## 六、日常维护链路

```bash
python -m causual.fetch                 # 抓数据（14 列自动）
python -m causual.regime fit            # 季度：拟合候选 + 迁移表（不生效）
python -m causual.regime promote --accept
python -m causual.edges fit             # 用当前标签重估所有边 → 快照
python -m causual.daily                 # 每天：秒级出卡片
```

`daily` 的三个拒绝条件：没有状态模型 / 没有边快照 / 签名不一致或快照超过 250 天。

---

# v3.1（2026-10-19）：滚动重估 / 汇率换源 / 读数可解释

## 汇总

| 类别 | 条目 |
|---|---|
| 新增 | 4（滚动重估、读数新鲜度标注、人民币同源、开盘价质量检查） |
| 修改 | 3（`train_end` 语义、覆盖率报告、卡片时效） |
| 删除 | 1（`COMEX_INVENTORY→GOLD` 边） |

## 一、新增

| 编号 | 项目 | 内容 | 为什么 | 影响 |
|---|---|---|---|---|
| C1 | **滚动重估** | 每 `refit_every_models=126` 个交易日重估一次，用当时可得的历史拟合，只对之后窗口出信号；共 15 次重估 | v3 之前只在训练期拟合一次，样本外 4 年后 β 早已漂移，卡片上写着"1436 天未重估" | 样本外从 952 天扩到 1823 天；最近重估日 2026-04-21（距今 178 天） |
| C2 | 读数新鲜度 | `Contribution.stale_days`，卡片把「前向填充导致的变化量为 0」显示成 `—（08-14 未更新）` | 之前显示 `0.000bp`，让人以为读数真的是 0 | 季频边与美盘变量不再误导 |
| C3 | 人民币同源 | `USDCNY` 改用中国银行折算价（每日、中国时区），`fetch_cny_boc()` | FRED DEXCHUS 是 NY noon 报价，实测滞后 3~7 天（最后一条停在 10-09），既让读数长期为 0，也可能是 `USDCNY→GOLD` 符号异常的根源 | 最后更新日 2026-10-16，与金价同日 |
| C4 | 开盘价质量检查 | 对比 AU0 期货开盘与上金所 Au99.99 开盘 | 判断 `fwd_oc` 的 0.41 是不是"开盘价=前收"造成的 | **结论：不是**。SGE 开盘等于前收的只占 5.3%，与 AU0 开盘的缺口相关性只有 0.35——说明 SGE 开盘**部分**反映隔夜外盘，剩余部分在日内补涨，这才是 `fwd_oc` 高 IC 的来源 |

## 二、修改

| 编号 | 项目 | 原来 | 现在 | 为什么 |
|---|---|---|---|---|
| C5 | `Panel.train_end` | 训练期截止日 | 最近一次重估日 | 语义随重估机制变化；卡片"模型时效"据此显示 |
| C6 | 样本外定义 | `index > train_end` | 所有有信号的日子（从首个重估窗口起） | 否则滚动重估后样本外只剩最后一个窗口 |
| C7 | 覆盖率报告 | 手动列恒显示"缺失" | 手动列清空后不再出现；`USDCNY` 来源标为"中国银行折算价" | 报告要反映真实来源 |

## 三、删除

| 编号 | 项目 | 理由 |
|---|---|---|
| C8 | `COMEX_INVENTORY → GOLD` 边 | 交割机制只影响短期挤仓，对中长期是噪音；CME 日报还需登录、无稳定公开接口。定义以注释形式保留，`COMEX_INVENTORY` 从必需列降级为可选 |

## 四、滚动重估后的结果

| 指标 | 单次拟合（v3） | 滚动重估（v3.1） |
|---|---|---|
| 样本外天数 | 952 | **1823**（2019-04 起） |
| Rank IC（收盘→收盘，次日） | 0.525 | 0.523 |
| Rank IC（开盘→收盘，次日） | 0.459 | **0.414**，CI [0.362, 0.464] |
| 最近重估距今 | 1436 天 | 178 天 |

两点解读：

1. **重估没有把信号打没**，说明 IC 不是"用了四年前的参数"造成的。
2. 但 `fwd_oc1` 仍有 0.41，这个量级对日频单品种来说**过高**。结合 C4 的检查，
   更像是「SGE 开盘滞后于隔夜外盘、日内补涨」的微观结构效应，而不是宏观因果信号。
   要定性需要分钟级数据，我暂时不把它当作可交易 alpha。

---

# v3（2026-10-19）：接入 WGC / CFTC / 国际金价

## v3 汇总

| 类别 | 条目 |
|---|---|
| 新增 | 6（XAU_USD 节点、COT_NET_LONG 节点、TECH_DEMAND 节点、WGC 季频解析、CFTC 周频抓取、开盘→收盘评估口径） |
| 修改 | 5（USDCNY 边的后门集、分布滞后支持季频、合成数据改为恒等式、信号时点、DAG 14→17 条边） |
| 删除 | 2（ETF_PREMIUM 边、ETF_FLOW 的日频口径） |

## 一、新增

| 编号 | 项目 | 内容 | 为什么 | 影响 |
|---|---|---|---|---|
| B1 | `XAU_USD` 节点 | COMEX 主力金价（`futures_foreign_hist("GC")`），2590 个交易日 | 国内金价 ≈ 国际金价 × 汇率 / 31.1035。缺这一项时 `USDCNY→GOLD` 被遗漏变量污染（v2 实测同日 −0.02、隔夜 −0.06） | 新增两条边；汇率边的后门集已包含它 |
| B2 | `COT_NET_LONG` 节点 | CFTC Legacy 报告，非商业净多头/总持仓（%），563 周 | 替代 ETF_PREMIUM：ETF 溢价长期贴 0，而持仓是真正的方向性拥挤 | 成为 associative 层的主要节点 |
| B3 | `TECH_DEMAND` 节点 | WGC 科技用金（季） | GDT 表里有，占比 ~7%，价格不敏感，可作对照组 | 新增季频边 |
| B4 | WGC GDT 解析 | `fetch.parse_gdt()` 按行号 + 季度列解析，66 个季度（2010Q1–2026Q2） | 央行购金/珠宝/科技/矿产/回收/ETF 六列一次拿全 | 5 个原来手动的列变成自动 |
| B5 | CFTC 抓取 | Socrata API 优先，403 时回退官方年度 zip（内含 annual.xls，126 列） | Socrata 对无 token 请求限流严重（403/400） | 563 周净多头占比，2016-01 至 2026-10 |
| B6 | 开盘→收盘评估 | `fwd_oc{h} = close_{t+h}/open_{t+1} − 1` | t 日信号含美盘信息（次日 05:00 才可得），只能从 t+1 开盘执行 | 主指标同时报 `fwd1` 与 `fwd_oc1` |

## 二、修改

| 编号 | 项目 | 原来 | 现在 | 为什么 |
|---|---|---|---|---|
| M1 | `USDCNY→GOLD` 的 confounders | `("USD_INDEX","REAL_YIELD_10Y")` | `("XAU_USD","USD_INDEX")` | 不控制国际金价，汇率效应与美元计价效应互相抵消 |
| M2 | 分布滞后频率 | 固定 `ME` | `Edge.freq` 可配，季频边用 `QE` | WGC 是季频；月频重采样只会把同一季度值重复三次，自由度虚高 |
| M3 | 合成数据的金价生成 | 独立随机游走 | `GOLD = XAU × USDCNY / 31.1035 ×(1+溢价)` 恒等式 | 让自检的识别结构与真实数据一致，否则会给虚假信心 |
| M4 | 信号时点 | 直接取 t 日面板 | `SIGNAL_LAG_DAYS`（当前 0），并明确执行时点是 t+1 开盘 | 见第四节 |
| M5 | DAG 规模 | 14 条边 | 17 条边 | 见上 |

## 三、删除

| 编号 | 项目 | 理由 | 替代 |
|---|---|---|---|
| D1 | `ETF_PREMIUM → GOLD` 边 | 国内黄金 ETF 规模大、溢价长期贴 0（实测 252 日 10%~90% 分位仅约 ±0.3pp），信息量不足 | `COT_NET_LONG`（方向性持仓） |
| D2 | `ETF_FLOW` 的日频口径 | 没有稳定的公开日频份额数据；GDT 只给季度净流量 | 季频 `ETF_FLOW`（吨/季） |

## 四、时区问题（v3 最重要的发现）

COMEX 金价、10Y 实际利率、美元指数、VIX、USDCNY 的 NY noon 报价，其「t 日」的收盘
都发生在上金所 15:30 收盘**之后**。三种口径的实测（真实数据，样本外 951 天）：

| 口径 | Rank IC（次日） | 说明 |
|---|---|---|
| t 日面板，收盘→收盘 | **0.53** | 用了当时还不存在的信息，虚高 |
| 面板右移 1 天，收盘→收盘 | **−0.03** | 只用 t−1 信息，把隔夜跳空整段切掉，过于保守 |
| t 日面板，开盘→收盘 | **0.44** | 对应「t+1 开盘执行」，但见下 |

**0.44 我仍不敢当作可交易 alpha**：上金所 Au99.99 早盘流动性薄，日开盘价经常等于前收，
「开盘→收盘」收益里混入了隔夜跳空——更像是**测量口径造成的假象**。要定论需要分钟级
数据或更严格的 session 对齐。在那之前，本框架的定位是**归因引擎**，不是预测模型。

## 五、季频数据的代价

央行购金、珠宝、科技、矿产、ETF 均为季频。前向填充后这些读数在季度内是常数，
分布滞后只有约 21 个季度观测、5 个参数，置信区间很宽（实测 CB 的 t 值在 1 附近）。
它们对 `alpha` 的贡献在季度内也是常数，只影响水平、不影响日频排序。
这是数据可得性的硬约束，不粉饰。

---

# v2（2026-10-18）：信号层重写与情绪入图

这份文件只回答一个问题：**跟上一版比，哪些地方被新增、修改、删除了，为什么**。
它不解释原理（那是 `SIGNAL.md` 的事），也不做使用说明（那是 `readme.md` 的事）。

每条都按同样的格式：**动机 → 改动 → 影响 → 怎么验证**。
v1 的原始代码已归档为项目根目录的 `legacy_v1_causual.py`，设计文档为 `readme_v1_design.md`。

---

## 汇总

| 类别 | 条目 | 其中会改变输出的 |
|---|---|---|
| 新增 | 9 | 6 |
| 修改 | 11 | 8 |
| 删除 | 5 | 4 |

**最重要的三条**：

1. `alpha` 从"regime 内常数"改成"逐日变化"（M1）——这是全部评估结果失效的根因。
2. `causal_forest` 分支原本**跑不通**（E2/E3），也就是说 v1 从未成功估出过任何一条边的效应。
3. 情绪因素以 `associative` 层进入 DAG（A1），与 `causal` 层在卡片上分开标注。

---

## 一、新增

| 编号 | 项目 | 内容 | 为什么 | 影响 | 验证 |
|---|---|---|---|---|---|
| A1 | 情绪/仓位两条关联边 | `OPEN_INTEREST → GOLD`（拥挤反转，−1）、`ETF_PREMIUM → GOLD`（境内溢价，+1）；`Edge` 新增 `layer` 字段 | 你希望情绪进图。但只有**非价格恒等**的代理才合法：持仓量与溢价率都不是收盘价的函数（溢价用 NAV），而 `CROWDING` 是 | 直接边从 9 条到 11 条；α 的约 18% 来自这两个 | 卡片第 2 节「层」列、第 6 节稳健性 |
| A2 | `layer` 字段 | `causal` / `associative` 二值 | 情绪类变量与金价**同期共同决定**，不能和"政策利率→实际利率"这种有时序先后的机制混为一谈 | 卡片分列；稳健性表可按层剔除 | `sign_audit()` 新增 layer 列 |
| A3 | 状态数由数据选 | `regime.select_n_states()`：BIC + Bhattacharyya，加 2% 简约容忍带 | 你的 note 里写"K 应数据决定"；论文硬编码 6、v1 硬编码 3 | 合成数据上选出 K=3；`--k` 可覆盖 | 运行时打印 BIC 表 |
| A4 | 发布延迟对齐 | `data.apply_publication_lag()`，9 个宏观变量按 `PUBLICATION_LAG_DAYS` 右移 | 在 t 日做决策只能用 t−lag 已发布的数据，否则是前视偏差 | 慢变量估计从"全样本泄漏"变为可用 | `data_freshness()` 输出滞后天数 |
| A5 | 合成数据 + 参数回收 | `data.make_synthetic()` 返回已知真值；`evaluate.recovery_table()` 比对 | 没有真实数据时，唯一能检验"估计器在估什么"的办法就是看它能否回收 DGP 系数 | 各边 95% CI 基本覆盖真值 | `--synthetic` 打印回收表 |
| A6 | Rank IC + 块 bootstrap | `evaluate.rank_ic_table()` / `rank_ic_stability()` | 你要 Rank IC；且日频收益强自相关，朴素 p 值过度乐观 | 输出 CI 与四段稳定性 | `rank_ic.csv`、`rank_ic_stability.csv` |
| A7 | 卡片「结论稳健性」节 | 全模型 / 仅因果层 / 仅关联层 / 仅显著边，四种口径的预期次日 | 辅助主观决策最需要知道"结论有多依赖某类证据" | 示例：全 −31bp，仅因果 −27bp（差 +4bp） | 卡片第 6 节 |
| A8 | 卡片四节新增内容 | 失效条件、反转所需读数、反事实敏感性、数据新鲜度 | 这四件是主观投资者真正会问的问题，v1 的 README 承诺了但没有实现 | — | 卡片第 7/8/9 节 |
| A9 | 模型时效提示 | 卡片第 1 节显示"拟合截止 X（距今 N 天）⚠ 建议重估" | 估计器只在训练期拟合一次，样本外 4 年后参数已漂移——必须让人看得见 | 演示卡片显示 1575 天 | 卡片第 1 节 |
| A10 | 真实数据抓取 `fetch.py` | FRED / 上金所现货 / 沪金主力 / 518880 / GPR 自动抓取；`data/cache` 缓存、失败回退旧缓存、`coverage.csv` 报告 | 没有数据，因果框架只是空转 | 2382 行 × 19 列：14 列自动、5 列待补 | `data/coverage.csv` |
| A11 | 手动数据模板 | `data/manual/*.csv` 五个模板自动生成，按日期合并前向填充 | WGC / CME / ETF 份额没有稳定的公开日频接口 | 缺列时对应边标「数据不可用」，不编造代理 | 卡片第 3 节 |
| A12 | HMM 特征降级 | 若某特征整列为空（如 `ETF_FLOW` 未补）则自动剔除并打印 | 否则 `dropna()` 清空整个样本，管线直接崩 | 真实首跑少了 `etf_flow_z`，其余 8 维照常 | 运行日志 |

---

## 二、修改

| 编号 | 项目 | 原来 | 现在 | 为什么 | 影响 |
|---|---|---|---|---|---|
| M1 | **信号聚合** | `alpha` 只依赖 regime 与全样本标量 → 全样本仅 3 个取值 | `contrib = m·β(regime)·z(t)`，逐日变化 | 论文的效应是 `f_ij(x,r)`，v1 把当期读数 `x` 丢了；这是建模错误 | 2347 个交易日里有 88 个不同 α；全部评估结果从"不可解读"变为可解读。详见 `SIGNAL.md` |
| M2 | **默认估计器** | `CausalForestDML` | `LinearDML`（`use_forest=True` 可切回） | 因果森林的单点 CATE 在 n≈1100、修饰变量只有 regime 时极不稳定：同一状态下换行会得到符号相反的效应，卡片敏感度会随噪声跳 | 系数稳定、带标准误、CI 可输出。恢复真值的能力反而更好（见回收表） |
| M3 | `CausalForest.fit` 的 X/W | `X=controls, W=effect_modifiers` | `X=regime 独热, W=混淆变量` | EconML 语义：X 是异质性切分特征，W 是控制变量。v1 传反了 | 森林原来按混淆变量切分，regime 异质性被当成控制项 |
| M4 | `effect()` 调用 | `effect(T0="auto", T1="auto")` | `effect(X=...)` / `const_marginal_effect` | `dT = T1 - T0` 对字符串会抛 `TypeError` | **v1 的该分支从未成功运行过** |
| M5 | 分布滞后被解释变量 | 价格水平 `GOLD_CNY` | 月度收益率 | 价格对价格回归是伪回归，系数没有经济含义 | 系数现在可读作"%/月" |
| M6 | 分布滞后的 p 值 | `model.pvalues[treatment]`（仅当期项） | 对**累积效应**做 Wald 检验 | 结论看的是累积和，p 值却对应单项 | 输出 `cumulative_ci` |
| M7 | 事件研究统计量 | 把所有重叠窗口的 AR 堆成一个数组求 t | 先按事件算 CAR，再对 CAR 序列检验（CAAR） | 原做法把同一事件的 4 天当成 4 个独立样本，t 值被事件数放大 | 示例：CPI→FED 由 t≈? 变为 t=4.86 |
| M8 | 事件的方向 | `abs(z) > 阈值`（双侧） | 单侧（`event_sign`：加息 / 通胀上行 / 地缘跳升） | 双侧把"加息"和"降息"当同一事件，符号会相互抵消甚至反转。实测过：修正前 CAAR=−0.06，修正后 +2.78 | 三条事件边的符号现在全部与理论一致 |
| M9 | 事件研究的市场基准 | `USD_INDEX` 当"市场" | 常数均值模型 | `USD_INDEX` 是黄金的**因果父节点**，拿它做市场基准等于控制了要估计的路径（bad control） | — |
| M10 | 慢变量/事件的训练窗口 | 全样本拟合 | 先裁到 `train_end` | 事件日期与月度末值都含未来信息 | 消除了两处泄漏 |
| M11 | `regime_valid` 的语义 | HMM 原始状态编号 `[0,1,2]` | 角色 `risk_on/transition/risk_off` | 原始编号随数据与初始化漂移，编号 2 今天可能是 Risk-On，明天是 Risk-Off | 门控语义稳定；`name_states` 不再硬编码 3 |

---

## 三、删除

| 编号 | 项目 | 删除理由 | 替代方案 |
|---|---|---|---|
| D1 | `VOLUME_Z → GOLD`、`CROWDING → GOLD` 两条边 | `CROWDING` = 20 日动量 z + 5 日波动 z，是 `GOLD_CNY` 的**恒等函数**，作为原因违反 DAG；`VOLUME_Z` 不是价格函数，但与 `\|收益\|` 同期强相关，只反映强度不反映方向 | 两者降级为**状态描述变量**：进 HMM 特征、卡片读数，不进 DAG。情绪因素改由 A1 的两条边承担 |
| D2 | 所有 position 相关 | 你要的是方向判断，不是仓位换算；仓位是另一层决策（风险预算、Kelly、止损） | α 保留为方向强度；`run_pipeline` 去掉 `max_position` 形参 |
| D3 | `from dowhy import CausalModel` | 导入后从未调用，是个假依赖，会让人误以为做了 do-calculus 识别 | 显式在限制里写明"未做形式化可识别性检验" |
| D4 | 逐边 z-score 后等权相加 | 每条边的 CATE 序列各自标准化后相加，量纲和经济含义全部丢失，`overall≈0` | 统一到"每 1σ → %"，按 `magnitude` 加权，单位回到 % |
| D5 | `plt.show()` 与图内中文标签 | 无 GUI 环境会阻塞；中文字体缺失会显示方块 | 输出 Markdown 卡片；绘图留待有字体环境再加 |

---

## 四、四个需要记录在案的决策

### 4.1 情绪为什么只能进 `associative` 层

你本意是"把情绪引入因果图"。技术上可以做，但要说清代价：

- 任何情绪变量（持仓、溢价、成交、热度）与金价都是**在同一时点被市场共同决定**的。
  DAG 的无环性在这里是名义上的——画箭头不等于有时间先后。
- 因此它估出来的 $\beta$ 不是 $do(\text{情绪})$ 的效应，而是"同期条件关联中滞后释放的那部分"。
- 处理方式：加 `layer` 字段、卡片分列、稳健性表单独剔一遍。
  这样情绪进了框架，但不会被当成和"实际利率→金价"同等级的证据。

**如果哪天你希望它升级为 `causal`**，前提是能给出一个"情绪外生冲击"的识别策略
（例如某类 ETF 的申赎规则变化、指数纳入事件），而不是靠同期数据。

### 4.2 为什么用 Rank IC 而不是 Pearson IC

- α 是**方向/趋势判断**，不是幅度预测。用 Pearson 会奖励"猜对大波动"，
  而惩罚"方向对但幅度保守"。
- Rank IC（Spearman）只看排序一致性，正好对应"这个信号能不能把未来的好坏日子排出来"。
- 单品种没有横截面，所以是**时序秩相关**；截面 IC 要等多品种。
- 附加块 bootstrap：日频收益自相关强，朴素 p 值会明显高估显著性。

### 4.3 为什么去掉了 position

- 你明确要求。从建模角度也站得住：仓位 = 信号 × 风险预算 × 约束条件，
  把它塞进信号层会让"信号好坏"和"仓位规则好坏"混在一起，无法归因。
- 现在 `summary.csv` 里仍有 `turnover`（信号换手），那是**信号稳定性**的度量，不是仓位。

### 4.4 为什么显著性不参与加权

11 条直接边里有 8 条 95% CI 覆盖 0，但它们的点估计照样进 α。三个选项里选了"不加权"：

- 只让显著边进 α → 用估计结果反过来筛模型，α 的方差更大、更难复现；
- 按 CI 宽度收缩 → 多一个需要解释的收缩强度参数；
- 不加权（现状）→ 无偏但噪声大。

代价是明确的：α 的相当一部分是噪声。所以卡片用「—」标出不显著，让读者自己判断。

---

## 五、还没做的（按优先级）

| 优先级 | 事项 | 说明 |
|---|---|---|
| 最高 | **补 `XAU_USD` 节点** | 真实数据暴露的识别缺口：国内金价 ≈ 国际金价 × 汇率，缺国际金价时 `USDCNY→GOLD` 系数被遗漏变量污染（实测同日 −0.02、隔夜 −0.06）。加一个节点 + 两条边（`XAU_USD→GOLD`、`XAU_USD→USDCNY` 或作为 FX 边的控制） |
| 高 | 补 5 列手动数据 | `CB_GOLD_BUY` `MINE_SUPPLY` `JEWELRY_DEMAND` `COMEX_INVENTORY` `ETF_FLOW`；补齐后 5 条边才可用 |
| 高 | 模型滚动重估 | 现在只在训练期拟合一次，卡片已提示"1436 天未重估" |
| 中 | 按 lag 分布逐日释放慢变量效应 | 现在用 ÷21 粗糙摊销（见 `SIGNAL.md` 3.2） |
| 中 | 形式化可识别性检验 + 反驳测试 | DoWhy identification + placebo / random common cause |
| 中 | 多跳路径积分 | 现在上游边不进 α，`FED→实际利率→金价` 的间接效应缺失 |
| 中 | 「拥挤反转」边待重写 | 真实数据里持仓量与金价同向（+0.17），与 DAG 假设相反，命中该边 falsifier |
| 低 | 事件聚类标准误 | 事件重叠时 CAAR 的 CI 偏窄 |
| 低 | 多品种（金银）与截面 Rank IC | 才能做真正的截面 IC |
| 低 | 交易成本与滑点 | 分层收益现在是毛收益 |
