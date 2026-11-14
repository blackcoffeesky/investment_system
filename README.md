# quant —— 量化研究工作空间

> **更新日期：2026-11-14**

一个**分层**的量化研究环境：数据、状态识别、因果归因、运维展示各自独立，
层与层之间只通过显式端口通信。

**不是**回测框架，也**不是**收益预测系统。它的产出是**辅助主观判断的卡片**。

> **多代理协作**：每个模块由独立代理负责，工作约束见 [`AGENTS.md`](AGENTS.md)。
> 四个模块代理 + 一个 orchestrator；代理只能看见别人的 `port.py`。

---

## 1. 模块地图

```
quant/
├── AGENTS.md        多代理协作的硬约束（先读这个）
├── shared/          跨模块纯工具（面板对齐 / 标准化 / 滚动 z）
├── datahub/         数据层：SQLite + 抓取入库 + /data 页面
├── state_layer/     状态层：市场观测 → 状态标签 + /state 页面（因果层的上游）
├── causual/         因果层：机制 → 方向 → 强度 → 卡片 + /cards /fit 页面
├── service/         框架层：注册表 + 共享模板 + 运维状态机
├── experiment/      试验场：K 值研究、API 探测（生产代码不依赖它）
├── tests/           跨模块测试
└── trash/           归档（不再使用的旧代码，留档备查）
```

| 模块 | 一句话 | 文档 |
|---|---|---|
| `datahub` | 把外部数据变成干净的表 | [README](datahub/README.md) · [DESIGN](datahub/DESIGN.md) |
| `state_layer` | 从市场观测识别"现在是什么状态" | [README](state_layer/README.md) · [DESIGN](state_layer/DESIGN.md) |
| `causual` | 每张卡说清"哪些机制在推、推多少" | [README](causual/doc/readme.md) · [DESIGN](causual/doc/DESIGN.md) |
| `service` | 一个入口访问全部能力 + 运维状态机 | [README](service/README.md) · [DESIGN](service/DESIGN.md) |

**`state_layer` 是 `causual` 的上游**：状态层回答"现在像什么"，因果层回答"为什么"。
因果层把状态当数据用，**不 import 状态层内部**——状态以 `state` 字典传入。

---

## 2. 模块模型与依赖方向

每个模块 = **核心代码 + `web/` + `port.py`**：

```
datahub/      web/   port.py(DataPort)    fin/ ops/
state_layer/  web/   port.py(StatePort)   model/ state_assets/ analysis/
causual/      web/   port.py(CausalPort)  causual_core/ causual_assets/
service/      app.py registry.py ops/(状态机) templates/(共享骨架)
```

模块注册时声明 `service_port_factory`，service 把它收进 `ServiceBag` —— **注册即共享**。

| 场景 | 允许 |
|---|---|
| service 内部（状态机、app、路由） | 必须走端口：`ctx.services["datahub"]` |
| 模块 web（自己的页面） | 只能调**自己**的 port，跨模块走 `request.app.state.services` |
| 模块内部 | 只能调自己的实现 |
| `*/cli.py` | 允许直接 import 别的模块的 **port**（命令行是装配点） |
| 任何模块 `import` 别人的内部实现 | **禁止**（`tests/test_decoupling.py` 会拦） |

---

## 3. 跑起来

```bash
# 数据
python -m datahub.fin.update                  # 抓数（低频表自动跳过）
python -m datahub.fin.check                   # 完整性检查

# 状态模型（改特征/K 之后）
python -m state_layer.cli regime --asset ashare

# 之后必须重估边，否则卡片会被拒
python -m causual.causual_core.cli edges --asset ashare

# 出卡
python -m causual.causual_core.cli daily --asset ashare

# 服务
python -m service.app
```

### 合并前必跑

```bash
python tests/run_tests.py
pyright --project pyrightconfig_off.json
```

`pyrightconfig_off.json` 平时关掉（否则顶掉 Pylance 的提示），检查时用 `--project` 指定。

---

## 4. 日常使用

```bash
# 1) 更新数据（自动源）
python -m datahub.fin.update

# 2) 检查完整性
python -m datahub.fin.check

# 3) 出卡
python -m causual.causual_core.cli daily --asset ashare
python -m causual.causual_core.cli daily --asset gold

# 4) 起服务（浏览器看卡片与运维）
uvicorn service.app:app --host 127.0.0.1 --port 8000
```

或者干脆只起服务，在 `/ops` 页面点「运行一次 / 重跑」——那会按状态机
依次执行 fetch → validate → generate_cards → generate_macro。

手工数据（WGC 季报、ETF 月报）走 `datahub.fin.intake`：

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
| `state_layer/saved_models/<asset>/` | 状态模型产物（spec / labels / chain）**不进 git** |
| `causual/data/snapshots/` | 边模型快照 |
| `causual/data/output/{asset}/cards/YYYY/MM/` | 卡片（按年月归档） |
| `.env` | API key 等（**已在 gitignore**） |
| `tests/run_tests.py` | 不依赖 pytest 的测试入口 |

---

## 8. 当前状态（2026-11-14）

- 数据层：45+ 张表；四种表结构（single/multi/long/static）；接入 ashareapi
- 状态层：模型链 `extreme → hmm`；K=3；`/state` 页
- 因果层：A 股 10 条边、黄金 9 条边；卡片只留信号层 + 一行状态
- 服务层：卡片浏览、运维状态机、消息确认、静音、看门狗
- 测试：64 个，全部通过；pyright 0 error

**已知短板**：`industrial_yoy`/`exports_yoy` 停在 2025-08；`geopolitic_risk` 源停更；
退市股缺失（幸存者偏差）；K=3 有退化状态（见 `experiment/`）。

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
| `causual/data/`（边快照、状态标签） | `python -m causual.causual_core.cli regime --asset ashare` 再 `cli edges` |
| `regime_engine/saved_models/`（HMM pkl） | 同上，`cli regime` 会写出来 |
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

---

## 11. 更新服务器

服务器拉 GitHub 偶尔会超时（国内网络）。稳妥做法是用 bundle 传：

```bash
# 本地
git bundle create /tmp/upd.bundle main
scp /tmp/upd.bundle linux-server:/tmp/upd.bundle

# 服务器
cd ~/workspace/quant && git pull /tmp/upd.bundle main
systemctl --user restart quant
```

模型产物（`causual/data/`、`regime_engine/saved_models/`）**不在仓库里**，
换机器时要单独传：

```bash
tar czf - causual/data regime_engine/saved_models | ssh linux-server "cd ~/workspace/quant && tar xzf -"
```

数据库同理（WAL 模式，必须先合并）：

```bash
python -c "import sqlite3; s=sqlite3.connect('datahub/datahub.sqlite'); d=sqlite3.connect('/tmp/up.sqlite'); s.backup(d)"
scp /tmp/up.sqlite linux-server:~/workspace/quant/datahub/datahub.sqlite
```
