# quant —— 量化研究工作空间

> **更新日期：2026-11-28**

一个**分层**的量化研究环境：数据、状态识别、因果归因、运维展示各自独立，
层与层之间只通过显式端口通信。

**不是**回测框架，也**不是**收益预测系统。它的产出是**辅助主观判断的卡片**。

> 多代理协作：每个模块由独立代理负责，硬约束见 [`AGENTS.md`](AGENTS.md)。
> 代理只能看见别人的 `port.py`，跨模块接口改动必须先报 orchestrator。

---

## 1. 模块地图

```
quant/
├── AGENTS.md        多代理协作的硬约束（先读这个）
├── shared/          跨模块机制：utils/（交易日历·标准化·单位原语）+ adapter/（唯一取数入口）
├── datahub/         数据层：SQLite + 抓取入库 + /data 页面
├── state_layer/     状态层：市场观测 → 状态标签 + /state 页面（因果层的上游）
├── causual/         因果层：机制 → 方向 → 强度 → 卡片 + /cards /fit 页面
├── service/         框架层：注册表 + 共享模板 + 运维状态机
├── tests/           跨模块测试（按模块分目录）
├── task/            任务日志与决策（归档在 task/archive/）
├── experiment/      试验场——**不进版本库**（.gitignore），磁盘上保留
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

## 2. 数据只走一条路

```
datahub（表 / 派生量）  →  shared/adapter.get_data(Panel)  →  DataFrame  →  装配点  →  核心代码
```

- `get_data()` 是**全系统唯一取数入口**，只在装配点调：`*/cli.py`、`*/web/routes.py`、`service/`
- `port.py` / `estimator.py` / `features.py` **只收 DataFrame**，不自己取数
  （`tests/cross/test_layering.py` 静态钉住）
- 面板由 `shared/adapter` 统一声明：`Panel.STATE_ASHARE` / `STATE_GOLD` / `CAUSUAL_*`。
  **要新列就改 adapter 的声明，不要在模块里自己拼表。**

```
原语（diff_bp / rolling_z …）              → shared/utils/data_processing
通用业务派生（breadth / etf_dispersion …）   → datahub/fin/derive.py 的 DERIVED 注册表
资产专属特征                                 → state_layer/state_assets/<asset>/features.py
```

---

## 3. 依赖方向

每个模块 = **核心代码 + `web/` + `port.py`**：

```
datahub/      web/  port.py(DataPort)     fin/ ops/
state_layer/  web/  port.py(StatePort)    model/(hmm,thmm) state_assets/(base,ashare,gold) profile.py
causual/      web/  port.py(CausalPort)   causual_core/ causual_assets/
service/      app.py registry.py ops/(状态机) templates/(共享骨架)
```

| 场景 | 允许 |
|---|---|
| service 内部（状态机、app、路由） | 必须走端口：`ctx.services["datahub"]` |
| 模块 web（自己的页面） | 只能调**自己**的 port，跨模块走 `request.app.state.services` |
| `*/cli.py`、`*/web/routes.py` | 装配点：取数、调别人的 port，把 DataFrame 喂给核心代码 |
| 任何模块 import 别人的内部实现 | **禁止**（`tests/cross/test_decoupling.py` 会拦） |

---

## 4. 跑起来

```bash
# 数据
python -m datahub.fin.update                  # 抓数（重试 transient 失败；低频表自动跳过）
python -m datahub.fin.check                   # 完整性检查（缺口 / 尾部停更 / 非交易日脏数据）

# 状态模型（改特征 / 改窗口之后）
python -m state_layer.cli fit --asset ashare
python -m state_layer.cli fit --asset gold

# 状态标签变了就必须重估边，否则卡片会被签名守卫拒绝
python -m causual.causual_core.cli edges --asset ashare
python -m causual.causual_core.cli edges --asset gold

# 出卡
python -m causual.causual_core.cli daily --asset ashare

# 服务（Web + 运维状态机）
python -m service.app
```

### 合并前必跑

```bash
python tests/run_tests.py                          # 全量，约 6 分钟
python tests/run_tests.py --fast                   # 开发中迭代（跳过慢文件）
python tests/run_tests.py state_layer causual      # 只跑指定组
python -m pyright --project pyrightconfig_off.json # 必须 0 error
```

`pyrightconfig_off.json` 平时关掉（否则顶掉 Pylance），检查时用 `--project` 指定。

---

## 5. 状态层口径（2026-11-23 重构后）

| 资产 | 分组 | 模型 | K | 标签 |
|---|---|---|---|---|
| A 股 | `short_price`（**primary**） | Student-t HMM | 3 | 下跌 / 震荡 / 上涨 |
| A 股 | `short_vol` | Gaussian HMM | 3 | 低波 / 中波 / 高波 |
| A 股 | `long_price` | Student-t HMM | 2 | 下跌 / 上涨 |
| 黄金 | `raw` | Gaussian HMM | 4 | trend_down / range / trend_up / distribution |

滚动窗口：A 股 `min_train=1000 / refit_every=125 / window=1000`；黄金 `500/60/250`（钉死，未改）。

**两条契约（加组时必须遵守）**：

1. `label_signature` = primary 组「日期:标签」的指纹 → **加组不改签名**，因果层只读 primary
2. 换特征 / 换窗口 = 换标签体系 → 签名变 → **必须重估 `cli edges`**，否则卡片被拒

`/state` 页面按资产分页，支持 `?lo=&hi=` 调整显示窗口（服务端渲染，无前端 JS）；
图上阴影带标出"当前拟合所用训练窗口"，阴影右侧是**外推**（模型未重拟合的那几天）。

---

## 6. 运维

### 6.1 代码只在本地改，服务器只跑不写

```
1. 本地改  2. 本地验证（tests + pyright）  3. push GitHub  4. 服务器 pull + 重启
```

服务器拉 GitHub 偶尔超时，稳妥做法是 bundle 中转：

```bash
# 本地
git bundle create "$TEMP/upd.bundle" main
scp "$TEMP/upd.bundle" linux-server:/tmp/upd.bundle

# 服务器
cd ~/workspace/quant && git pull --ff-only /tmp/upd.bundle main
systemctl --user restart quant
```

### 6.2 产物不进 git，要单独传

`state_layer/saved_models/`、`causual/data/`（边快照 + 卡片）、`datahub/datahub.sqlite` 都在 `.gitignore` 里。

```bash
tar czf "$TEMP/artifacts.tar.gz" state_layer/saved_models causual/data/snapshots causual/data/output
scp "$TEMP/artifacts.tar.gz" linux-server:/tmp/
ssh linux-server "tar xzf /tmp/artifacts.tar.gz -C ~/workspace/quant"
```

### 6.3 数据库备份

WAL 模式，直接复制文件可能丢最近事务：

```bash
python -c "import sqlite3;s=sqlite3.connect('datahub/datahub.sqlite');d=sqlite3.connect('/tmp/bak.sqlite');s.backup(d)"
```

---

## 7. 仓库里没有的东西

| 缺什么 | 怎么补 |
|---|---|
| `datahub/datahub.sqlite` | `python -m datahub.fin.update` |
| `state_layer/saved_models/` | `python -m state_layer.cli fit --asset {ashare,gold}` |
| `causual/data/`（边快照、卡片） | `cli edges` 再 `cli daily` |
| `experiment/` | 试验场，不进版本库；换机器从旧机器拷 |

---

## 8. 最近的变化

当前进行中的任务日志在 `task/`，历史归档在 `task/archive/`（带日期与摘要，**归档不删**）。

| 日期 | 主题 |
|---|---|
| 2026-11-28 | 删除 `shibor3m` 整条链路（表/源/边）；更新失败按 transient/structural 分级 |
| 2026-11-24 | 状态层三组 + 服务端窗口渲染（删前端 JS）；`find_gaps` 加 `non_trading` |
| 2026-11-23 | 状态层重构（链 → 多分组）、全库清旧、因果层重估 |
| 2026-11-22 | 数据时效性 + `check` 重构（日历→`shared`、写守卫→`sources`） |
