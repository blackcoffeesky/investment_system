# AGENTS —— 所有模块代理必读

> 这份文件是**硬约束**，不是建议。四个模块代理 + 一个 orchestrator 都按它工作。
> 更新日期：2026-11-14

---

## 1. 分工

| 角色 | 目录 | 能碰的东西 |
|---|---|---|
| `datahub-agent` | `datahub/` | 数据层：抓取、入库、完整性检查、`/data` 页 |
| `state-layer-agent` | `state_layer/` | 状态层：特征、模型链、状态标签、`/state` 页 |
| `causual-agent` | `causual/` | 因果层：边模型、信号层、卡片、`/cards` `/fit` 页 |
| `service-agent` | `service/` | 框架层：注册表、状态机、共享模板 |
| `orchestrator` | 根目录 | 合并、跑检查、裁决跨模块接口 |

**公共区**：`shared/`（纯工具）、`tests/`（跨模块测试）、`experiment/`（试验场）、`AGENTS.md`。

`shared/` 只允许**纯函数**：不读库、不碰文件、不 import 任何业务模块。改它要报 orchestrator。

---

## 2. 三条硬约束

### ① 只能看见别人的 `port`

```python
# ✅ 允许
from datahub.port import DataPort          # 类型
ctx.services["datahub"]                    # 运行期（service 内）
request.app.state.services["state"]        # 运行期（web 内）

# ❌ 禁止
from datahub.fin.read import read          # 摸到别人内部
from state_layer.model.hmm import GaussianHMM
from causual.causual_core.pipeline import run
```

**唯一的例外是 CLI**：`*/cli.py` 允许直接 import 别的模块的 port（命令行是装配点）。
即便如此，也只准用 port，不准用内部实现。

### ② 不跨目录改文件

一个代理只改自己模块下的文件。**需要别人改** → 发消息给 orchestrator，说清楚：

- 要改哪个函数/文件
- 为什么必须改
- 影响面（谁在调）

### ③ 跨模块接口改动必须先报

端口方法增删、参数变化、返回值结构变化，都属于接口改动。**先报再改**。
理由：上次 `state` 端口加 `causual_view` 时，causual 侧没同步，卡片直接挂。

---

## 3. 合并前必跑三件套

```bash
python tests/run_tests.py                    # 必须全绿
pyright --project pyrightconfig_off.json     # 必须 0 error
# test_decoupling 已包含在 run_tests 里：它扫 import，防止跨模块直连
```

`pyrightconfig_off.json` 名字带 off，是因为平时要关掉（否则顶掉 Pylance）。
**不要改名**，用 `--project` 指定即可。

---

## 4. 设计准则（各模块通用）

| 准则 | 含义 |
|---|---|
| **一个文件一件事** | 不要新建"什么都有"的模块 |
| **数据只在一处定义** | 表结构、字段类型、常量：一份。副本必然漂移 |
| **纯函数优先** | 能写成 `输入 → 输出` 的，就不要写带状态的类 |
| **不藏副作用** | 函数名管什么就干什么；写库的函数不叫 `load` |
| **失败要响** | 缺数据可以降级，但必须打印清楚；不要静默返回空 |
| **不留临时方案** | 临时可以，但要在文件头和提交信息里写明"临时、将来怎么改" |

### 目录纪律（最容易漂的地方）

改完代码，回头对一遍自己模块的 `DESIGN.md` 里的"文件职责"表。
**表里没有的新文件，就是漂了**——要么补表，要么别建。

---

## 5. 沟通规则

| 情况 | 动作 |
|---|---|
| 要改别人的接口 | 先发消息给 orchestrator，附影响面 |
| 卡超过 30 分钟 | 发消息说明卡点，不要闷头试 |
| 发现别人的 bug | 不要顺手改，报告 + 给出复现方式 |
| 需要新增公共工具 | 提议放 `shared/` 还是自己模块内，说理由 |
| 完成一个阶段 | 汇报：改了什么 / 验证结果 / 遗留问题 |

---

## 6. 已知陷阱（都是踩过的）

1. **跨资产复制常量块** → gold 抄了 A 股的 `SIGNAL_LAG`，α 从 +0.01 变 -0.38。逐项核对。
2. **pandas 的 `x or default`** → Series 真值有歧义，必须 `if x is None`。
3. **`fin.read` 同名遮蔽** → `from datahub.fin import read` 拿到的是**函数**，不是模块。
4. **删目录连带删产物** → `saved_models/` 在 gitignore 里，`rm -rf` 会连模型一起删。
5. **模型产物不进 git** → `state_layer/saved_models/`、`causual/data/` 部署时要单独搬。
6. **改特征 = 换标签体系** → `label_signature` 变了，边快照全部失效，必须重估 edges。
7. **`.env` 的 BOM** → PowerShell 写入带 BOM，第一个键名会变成 `\ufeffKEY`。
8. **改标识格式要全库 grep** → 2026-11-14 把 ETF 代码从裸码 `510300` 改成 `sh510300` 时，
   `state_layer` 和 `causual` 里各有一处"按裸码做字符串匹配"的写法（`GROUPS` 硬编码 + `startswith("c")`），
   结果**一列特征变 NaN、几列派生量整体消失**，全程没有任何报错。
   改任何标识（代码 / 表名 / 列名）之前，先 `grep` 一遍有没有按它做字符串匹配的地方。

---

## 7. 提交纪律（orchestrator 尤其注意）

**代理干活期间，orchestrator 不要跑 `git add -A`。**

2026-11-14 踩过：datahub-agent 正在写 `derive.py`，orchestrator 同时提交文档修改，
`git add -A` 把代理的半成品一起卷进了自己的 commit。没丢数据，但两个 commit
的边界糊了，事后要逐个比对才知道谁改了什么。

**第二次同类事故（2026-11-14 晚）**：`datahub-agent` 用 `git rm` 暂存了 `sources.py` 的删除，
orchestrator 随后 `git add <causual 的文件>` 再 `git commit` —— **`git commit` 提交的是整个索引**，
把别人暂存的删除一起带走了。结果 `fbe207f` 到 `84f36df` 之间，HEAD 上 `datahub/fin/sources` 完全不存在，
**从 HEAD 检出的环境 import 直接失败**。

正确做法（按可靠度排序）：

1. **`git commit -- <明确的路径>`** —— 路径限定的提交**只提交列出的文件**，别人的暂存区不会被带走（首选）
2. 提交前必看 **`git diff --cached --name-only`**，确认暂存区里只有自己的文件
3. 永远不用 `git add -A` / `git commit -a`
4. 只 `git add <自己的文件>` 是**必要但不充分**的——它挡不住别人之前已经暂存的东西

代理之间并行时，各自负责的目录是**不相交**的，看到别人目录的文件被暂存就该停下来问。

## 8. 提交规范

```
<模块>: <一句话做了什么>

- 具体改动（为什么这么改）
- 影响面（谁的调用点变了）
```

**文档同步是提交的一部分**：改了行为就要改 `DESIGN.md`，改了用法就要改 `README.md`。
只改代码不改文档的提交，会被打回。

---

## 9. 每个模块的文档在哪

| 模块 | DESIGN | README |
|---|---|---|
| datahub | `datahub/DESIGN.md` | `datahub/README.md` |
| state_layer | `state_layer/DESIGN.md` | `state_layer/README.md` |
| causual | `causual/doc/DESIGN.md` | `causual/doc/readme.md` |
| service | `service/DESIGN.md` | `service/README.md` |

改动前先读自己模块的 DESIGN，改动后更新它。
