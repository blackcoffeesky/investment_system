# quant —— 量化研究工作空间

> **更新日期：2026-11-04**

一个**分层**的量化研究环境：数据、因果归因、状态识别、运维展示各自独立，
层与层之间只通过显式接口通信。

**不是**一个回测框架，也**不是**收益预测系统。它的产出是**辅助主观判断的卡片**。

---

## 1. 模块地图

```
quant/
├── datahub/         数据层：SQLite，一表一序列，负责抓取/校验/公布滞后
├── causual/         因果层：机制 → 方向 → 强度 → 卡片
├── regime_engine/   状态引擎：HMM 状态识别（不含任何资产知识）
├── service/         服务层：统一入口 + 运维状态机 + Web 展示
├── tests/           跨模块测试
└── trash/           归档（不再使用的旧代码，留档备查）
```

| 模块 | 一句话 | 文档 |
|---|---|---|
| `datahub` | 把外部数据变成干净的表 | [README](datahub/README.md) · [DESIGN](datahub/DESIGN.md) |
| `causual` | 每张卡说清"哪些机制在推、推多少" | [README](causual/doc/readme.md) · [DESIGN](causual/doc/DESIGN.md) |
| `regime_engine` | 市场现在处于什么状态 | 见 `regime_engine/` 源码注释 |
| `service` | 一个入口访问全部能力，另有运维状态机 | [README](service/README.md) · [DESIGN](service/DESIGN.md) |

---

## 2. 依赖方向（硬约束）

```
service  ──→  causual  ──→  datahub
   │            │
   └────────────┴──→  regime_engine
```

- **数据层不依赖任何上层**：`datahub` 里 import `causual` 是方向错误
- **因果层不取数**：`causual_core` 里没有任何 `requests` / `akshare` / `db.read`
- **模块之间不直接 import**：跨模块一律走 `service.DataService`
- **只有 `signal` 层进 α**：结构层与供需层是背景，不是当日归因

违反这些约束的改动，就算能跑通也算 bug。

---

## 3. 数据流

```
外部源（FRED / akshare / CFTC / WGC 文件 / XAUS / WTM）
        │  datahub.update / datahub.intake
        ▼
datahub.sqlite   （一表一序列 + metadata + missing + ops_*）
        │  datahub.fin.read  ←  service.DataService
        ▼
causual_assets/{ashare,gold}/panel.py     列名映射 + 公布滞后
        ▼
causual_assets/{ashare,gold}/pipeline.py  特征 + 供需层
        ▼
causual_core: signal（α）/ top_layer（位置×边际）/ narrative（卡片）
        ▼
causual/output/{asset}/cards/YYYY/MM/card_YYYY-MM-DD.md
        ▼
service.app（FastAPI）→ 手机/浏览器
```

---

## 4. 日常使用

```bash
# 1) 更新数据（自动源）
python -m datahub.fin.update

# 2) 检查完整性
python -m datahub.fin.check

# 3) 出卡
python -m causual.causual_core.daily --asset ashare
python -m causual.causual_core.daily --asset gold

# 4) 起服务（浏览器看卡片与运维）
uvicorn service.app:app --host 127.0.0.1 --port 8000
```

或者干脆只起服务，在 `/ops` 页面点「运行一次 / 重跑」——那会按状态机
依次执行 fetch → manual_check → validate → generate_cards。

手工数据（WGC 季报、ETF 月报）走 `datahub.intake`：

```bash
python -m datahub.fin.intake wgc <xlsx> --load
python -m datahub.fin.intake wgc_etf <xlsx> --load
```

---

## 5. 三条设计原则

**① 数据层不为使用方考虑。**
表名按数据本身取（`csi_all` 而不是 `ashare_close`），一份数据被多方使用。

**② 派生量不入库。**
判据是"去掉它之后还能不能用库里其他数据算出来"。算得出来的（ETF 分流、
央行增持、卡片流量）由消费层现算。

**③ 只给可复现的量。**
不做综合合成、不做"传导结构"这类测不准的东西；没有数据的边标「未估计」，
不编造代理值。

---

## 6. 环境

- Python：`D:\desktop\workspace\global_venv\Scripts\python.exe`（3.14）
- 依赖：`pandas` `numpy` `akshare` `econml` `pandas-datareader`
  `fastapi` `uvicorn` `markdown` `python-multipart` `httpx2`
- 网络：FRED / CFTC 需代理（默认 `127.0.0.1:7890`）；东财等国内接口直连
- 数据库：`datahub/datahub.sqlite`（WAL 模式，备份用 `sqlite3 .backup`）

---

## 7. 目录约定

| 路径 | 内容 |
|---|---|
| `datahub/datahub.sqlite` | 全部数据 + 运维状态 |
| `datahub/intake/{raw,clean,parsers}` | 手工数据的暂存/解析 |
| `causual/data/{regime,snapshots}` | 模型产物（标签、边快照） |
| `causual/output/{asset}/cards/YYYY/MM/` | 卡片（按年月归档） |
| `tests/run_tests.py` | 不依赖 pytest 的测试入口 |

---

## 8. 当前状态（2026-11-04 阶段性封顶）

- 数据层：36 张表，自动源 29 个、手工源 2 个
- 因果层：A 股 10 条边、黄金 9 条边；供需层按「位置 × 边际」呈现
- 服务层：卡片浏览、运维状态机、消息确认、静音、看门狗
- 测试：35 个，全部通过

**已知短板**（详见各模块文档）：`industrial_yoy`/`exports_yoy` 停在 2025-08、
`geopolitic_risk` 源已停更、央行季报有 23/87 国未更新。

---

## 9. 部署

```bash
git clone git@github.com:blackcoffeesky/investment_system.git
cd investment_system

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 建库并抓数（首次会跑几分钟；周期源首次会拉全量历史，耐心等）
python -m datahub.fin.update

# 拟合状态模型 + 边快照（仓库不含这些产物，见下）
python -m causual.causual_core.cli regime --asset ashare   # HMM 状态（含极端规则）
python -m causual.causual_core.cli edges  --asset ashare   # 重估所有边 → 快照
python -m causual.causual_core.cli audit  --asset ashare   # 方向一致性审计

# 出卡
python -m causual.causual_core.daily --asset ashare

# 起服务
uvicorn service.app:app --host 0.0.0.0 --port 8000
```

**仓库里没有的东西**（`.gitignore` 排除，需自行生成）：

| 缺什么 | 怎么补 |
|---|---|
| `datahub/datahub.sqlite` | `python -m datahub.fin.update` |
| `causual/data/`（边快照、状态模型） | `python -m causual.causual_core.cli regime --asset ashare` 再 `cli edges` |
| `causual/output/`（卡片） | 跑 `daily` 或服务的 `/ops` → 运行一次 |
| `datahub/fin/intake/raw/*.xlsx` | 手工源的原始文件，需自行放回 |

`datahub/fin/intake/clean/*.csv` **是包含的**——它是解析后的手工数据，
有了它才能在不放回 xlsx 的情况下重建库。

---

## 10. 备份

数据库是 WAL 模式，`datahub.sqlite` 旁边还有 `-wal`/`-shm`。
**直接复制文件可能丢最近事务**，正确做法：

```bash
sqlite3 datahub/datahub.sqlite ".backup backup/db-$(date +%F).sqlite"
```
