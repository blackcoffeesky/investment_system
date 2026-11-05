# datahub 设计文档

> **更新日期：2026-11-04**　｜　使用说明见 [`README.md`](README.md)

---

## 1. 一句话定位

**把五花八门的外部数据，变成一张张"一看就懂、拿来就用"的表。**
它不判断数据有没有用、不计算派生指标、不关心谁在消费。

三条硬约束：

1. **一个表 = 一条（或一组同源同频的）序列**
2. **除 `db.read` 外不对外暴露写接口**
3. **数据层不依赖任何上游包**（`causual` 不能反过来被 import）

---

## 2. 分层与数据流

```
            外部世界
   FRED / akshare / CFTC / XAUS / WTM / WGC 文件
                    │
        ┌───────────┴────────────┐
        │  sources.py (SOURCES)  │  自动源注册表
        │  intake/ (parsers)     │  手工源解析
        └───────────┬────────────┘
                    │  update.py / intake/cli.py
                    ▼
          datahub.sqlite
          ├── metadata   每个变量的元信息
          ├── missing    已确认的正常缺口
          └── <表>       一表一序列
                    │
                    │  db.read()
                    ▼
              任何消费方
```

**关键分界**：`sources.py` 只负责"从哪拿"，`update.py` 只负责"怎么写"。
两者之间只有 `Series / DataFrame` 一种约定，没有别的情感纠葛。

---

## 3. 文件职责

```
datahub/
├── __init__.py            只放 ROOT / DB_PATH / __version__
├── core/
│   ├── conn.py            connect / transaction / connection / exec_ddl
│   ├── schema.py          DATA_DDL 与 OPS_DDL（分开写，同文件）
│   └── errors.py          DatahubError / TableNotFound / OpsTableAccess / SchemaError
├── fin/
│   ├── __init__.py        对外：read / read_many / metadata / tables / check / missing
│   ├── read.py            读接口
│   ├── update.py          按 SOURCES 更新 + 元数据刷新
│   ├── sources.py         取数函数 + SOURCES 注册表
│   ├── check.py           交易日历 + 完整性检查
│   ├── intake/            raw / clean / parsers / registry / cli
│   └── import_panels.py   一次性建库（遗留）
└── ops/
    ├── __init__.py        对外：OpsStore
    └── store.py           状态 + 消息，单事务 save_result
```

| 文件 | 职责 | 谁调用 |
|---|---|---|
| `core/conn.py` | 连接超时、事务边界、DDL 执行 | fin / ops |
| `core/schema.py` | 所有 DDL 的唯一出处 | update / intake / ops |
| `fin/read.py` | 只读 API | 服务层 / 脚本 |
| `fin/sources.py` | "从哪拿" | update |
| `fin/update.py` | "怎么写"（upsert + 元数据） | 人 / 服务层 |
| `fin/check.py` | 缺口判断（日历驱动） | 状态机 / 人 |
| `ops/store.py` | 运维状态持久化 | Manager |

**隔离原则**：`ops` 不 import `service`（消息以 dict 传入）；
`fin.tables()` 只读 `metadata`，所以 `ops_*` 永远不会混进金融表清单。

## 4. 关键设计决定

### 4.1 为什么一表一序列，而不是宽表

宽表（一行日期、几十列）看着省事，但：
- 每列的频率不同（日/周/月/季），宽表必然要 ffill，**ffill 出来的假行会污染分位与差分**
- 加一列要改表结构，SQLite 的 `ALTER TABLE` 又笨

代价是表多（当前 36 张），但每张表的语义单一，`SELECT * FROM vix` 就是全部。

**例外**：同源、同频、同生命周期的多列才合并（OHLCV、ETF 份额）。

### 4.2 为什么频率要还原

历史面板是日频 ffill 过的，月频变量每天一行。若原样入库，`cpi_yoy` 就成了"日频"，
之后任何 `resample` 或分位计算都会错。所以 `import_panels._resample` 把它还原成月末一行，
并且**截断尾部 ffill 撑出来的假行**（源数据 8 月停了，9/10 月仍被填成 8 月的值）。

### 4.3 为什么要有 `lag_days`

「2026-09-30 的 CPI」指的是**9 月 CPI**，但它 10 月 9 日才公布。
如果消费层在 9-30 就用它，回测会偏乐观。`lag_days` 把这个"数据期 → 可见日"的滞后
变成数据层的属性，消费层照着推后即可。

### 4.4 为什么检查要用交易日历

"缺行"有两种：真的漏了，市场本来就没开市。用 SSE/NYSE 日历把后者排除后，
剩下的才需要人看。否则 A 股每年 ~10 天长假会造成几十张表同时告警，告警就废了。

### 4.5 为什么 intake 要分 raw / clean / parsers

手工数据来源太杂（WGC 的 xlsx、东财的 csv、新闻里看到的一个数）。分开的理由：

- `raw/` 原样留档 → 半年后能回溯"当时文件长什么样"
- `clean/` 是可审阅的中间产物 → 入库前能人眼扫一遍
- 入库可重放 → 写错了删掉重跑

`clean/` 只放**要写的行**（新增 + 被修订），不是全量快照——否则无法区分"这次真的更新了"
和"又写了一遍没变的数"。

---

## 5. 日常调用顺序

### 5.1 每天（自动源）

```
python -m datahub.update
  │
  ├─ for s in SOURCES if s.enabled:
  │     obj = s.func(start=, end=, table=, **s.params)     # sources.py
  │     if "|" in s.table: 按列拆成多张单表
  │     _write(con, table, obj, end)                       # upsert，不删历史
  │     _apply_note(con, s, tables)                        # 把 note 落到 metadata
  │
  ├─ _ensure_meta(con)          # 有表没 metadata 的，补一条
  ├─ refresh_metadata(con)      # 重算 start/end/n_rows
  └─ 失败 → data/missing/update_failures.log
```

### 5.2 每月/每季（手工源）

```
① 把文件丢进 intake/raw/
② python -m datahub.intake wgc <file> --load
     ├─ registry.SOURCES["wgc"].parser(raw)                # 解析
     ├─ diff_to_write(new, 库里的旧值)                       # 只留新增 + 修订
     ├─ 写 intake/clean/<table>.csv
     └─ upsert 入库 + 刷新 metadata
③ 解析失败 → 提示"raw 的结构可能变了"，指出哪一环对不上
```

### 5.3 检查

```
python -m datahub.check
  ├─ 按 metadata.freq + market 生成"应有日期"（交易日历）
  ├─ 与实际日期做差
  ├─ 减掉 missing 表里已登记的缺口
  └─ 分「近期缺」与「历史缺口」两档报告
```

### 5.4 消费方

```
from datahub import db
s = db.read("vix")            # Series
d = db.read("csi_all")        # DataFrame
```

---

## 6. 扩展点

| 想加什么 | 改哪里 |
|---|---|
| 新自动源 | `sources.py` 加一个 `def xxx(start, end, table="", **_)`，然后在 `SOURCES` 加一行 |
| 新手工源 | `intake/parsers/` 加解析函数 → `registry.py` 注册一行 |
| 新元信息字段 | `schema.py` 的 `METADATA_DDL` + `update._ensure_meta` + `_apply_note` |
| 新市场日历 | `check._MARKET_ALIAS` |

**都不要改 `fin/read.py` 的公开函数签名**——读接口是稳定契约，改了所有消费方都要跟着动。

---

## 7. 已知的技术债

| 项 | 影响 | 建议 |
|---|---|---|
| `sources.fred` 里 `os.environ.setdefault` 设代理 | 全局副作用，可能污染其他请求 | 改成显式传 `proxies=` |
| `cfc` 查询 `$limit=50000` 硬编码 | CFTC 历史若继续增长会被截断 | 加分页或检测截断 |
| `check` 只检查「有没有这个日期」 | 不检查数值突变（如 10 倍跳变） | 加一层"与前值比值"的合理性检查 |
