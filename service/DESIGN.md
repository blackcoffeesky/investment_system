# service 设计文档

> **更新日期：2026-11-04**　｜　使用说明见 [`README.md`](README.md)　｜　项目总览见 [`../README.md`](../README.md)

---

## 1. 定位

服务层做两件事：

1. **唯一入口**：任何模块想知道"数据长什么样""谁是今天的状态"，都问 `DataService` 或走 HTTP。
2. **运维与展示**：状态机保证"数据更新 → 检查 → 出卡"这条链路每天走完，并把结果摊在网页上。

它**不含业务逻辑**——因果计算在 `causual`，取数在 `datahub`。service 只负责编排和呈现。

---

## 2. 进程模型

**单进程、单 uvicorn**。所有模块在 import 时注册进 `registry`，`app.py` 负责挂载。

```
uvicorn service.app:app
   │
   ├── import 时：_load_modules() → causual.web.register_module()
   ├── 启动时：_startup_recover()  ← 把上次崩在 running 的状态折算成 blocked
   └── 请求时：各路由
```

**状态机跑在后台线程**（`/task/run` 立刻返回，线程里跑 fetch），
否则一次 fetch 几分钟，HTTP 请求会一直挂着。

---

## 3. 文件职责

### 3.1 顶层

| 文件 | 职责 |
|---|---|
| `app.py` | FastAPI 实例；鉴权/日志/错误中间件；`/ops`、`/task/*`、`/health`；挂载各模块路由 |
| `registry.py` | 模块注册表 `Module` 数据类；汇总模板目录与导航项 |
| `data_service.py` | `DataService`：模块访问数据的唯一入口（read/metadata/check/update/intake） |
| `card_service.py` | `CardService`：生成/检查卡片，状态机通过 `ctx.extra["cards"]` 拿到 |
| `templates/base.html` | 全站骨架：导航 + 内容区 + 移动端 CSS（**所有模块共用**） |
| `templates/ops.html` | 运维页：运行按钮、状态机、消息分组、静音清单 |
| `templates/error.html` | 错误页（复用 base） |

**service 只留框架**：注册表、共享骨架、鉴权、错误处理、运行锁。
业务页面各回各家——`datahub/web/`（数据页）、`causual/web/`（卡片 + 拟合页）。

### 3.2 `ops/` —— 状态机

| 文件 | 职责 |
|---|---|
| `base_state.py` | `BaseState` ABC：`name / prefix / label / default_snooze`，`start/run/end` |
| `messages.py` | `MessageLevel` / `StateStatus` / `PASSABLE` / `Message` / `StateResult` |
| `actions.py` | `AckAction` 枚举 + `ALLOWED_ACTIONS`（error→retry，warning→retry/skip，info→ack） |
| `manager.py` | 编排：`run()` 按序执行、`ack()` 处理确认、`reset/reset_all()`、静音判定、运行日志 |
| `states/data.py` | `FetchState` / `ManualCheckState` / `ValidateState` |
| `states/causal.py` | `GenerateCardsState`：数据层三状态过后出卡 |
| `states/__init__.py` | `STATES` 列表——**加状态只改这一个文件** |

### 3.3 数据层里与 service 配套的部分

| 文件 | 职责 |
|---|---|
| `datahub/ops/store.py` | `OpsStore`：`ops_state` / `ops_messages` / `ops_snooze` / `ops_run_log` 的读写，`_guard()` 限制只能碰 `ops_*` |
| `datahub/core/conn.py` | 统一连接：WAL + `busy_timeout=30s`（**不要用裸 `sqlite3.connect`**） |

---

## 4. 状态机

```
fetch → manual_check → validate → generate_cards
```

每个状态：

```python
class XxxState(BaseState):
    name = "xxx"
    prefix = ("fetch",)             # 前置状态全部 passable 才跑
    label = "显示名"
    default_snooze = "days:7"       # 用户点"跳过"时的默认静音方式

    def run(self, ctx) -> StateResult:   # ctx.data_service / ctx.extra / ctx.logger
        ...
```

**状态**：`pending / running / done / blocked / waiting_ack / snoozed`
**可继续**（`PASSABLE`）：`done / snoozed`

失败语义：

| 情况 | 状态 | 消息 |
|---|---|---|
| 数据源不可用但仍在保鲜期 | `waiting_ack` | WARNING（可跳过） |
| 结构性错误 | `blocked` | ERROR（只能重跑） |
| 进程崩溃留下 `running` | `blocked` | ERROR `UNEXPECTED_INTERRUPT`，**不自动重跑** |
| 告警在静音期 | `snoozed` | 正常执行，只是不打断 |

---

## 5. 静音（snooze）

一条消息 = 一个 `(key, period)`，这是静音的最小单位。
`manual_check` 对**每张逾期表**单独发一条消息（key = 表名），所以静音 `cb_holdings`
不影响 `insider_net`。

| 模式 | 含义 | 恢复条件 |
|---|---|---|
| `once` | 只跳过本次 | 下次运行 |
| `days:N` | N 天内不提醒 | `remind_at` 到期 |
| `period_change` | 本期不再提醒 | 数据期（如 `end_date`）变化 |

**静音期内检查照常执行**（状态 `SNOOZED`），只是不打断——否则就变成"永久关闭"。

---

## 6. HTTP 接口

| 路径 | 方法 | 说明 |
|---|---|---|
| `/` | GET | 跳转 `/cards` |
| `/cards?asset=` | GET | 卡片列表（含「重新生成」按钮） |
| `/card/{asset}/{date}` | GET | 单张卡（`?kind=detail` 看详情） |
| `/card/{asset}/{date}/regenerate` | POST | 重新生成某天的卡 |
| `/ops` | GET | 运维页 |
| `/task/run` | POST | 触发一轮（`mode=run\|rerun`），后台线程执行，立即返回 |
| `/task/confirm` | POST | 确认消息（retry / skip + remind_at / ack） |
| `/health` | GET | `{ok, modules, journal_mode, busy_timeout}` |

**并发保护**：`RUN_LOCK`（非阻塞）——同一时刻只允许一个"会改状态"的操作。

---

## 7. 关键约束

1. **模块不互相 import**：跨模块一律走 `DataService`；`causual_core` 不许 import `service`
2. **页面加载不写库**：`Manager.__init__` 只读快照，恢复（`recover`）只在启动和 `run()` 开头
3. **长任务不上 HTTP 线程**：`/task/run` 起后台线程，页面轮询 `ops_run_log`
4. **消息结构不动**：展示层可以按 `(state, level)` 分组折叠，但 `Message` 仍是 `(key, period)` 粒度
5. **连接必须走 `core.conn.connect()`**：裸 `sqlite3.connect` 只有 5 秒超时，会撞锁

---

## 8. 扩展新模块

```python
# mymodule/web/__init__.py
def register_module():
    return register(Module(
        name="mymodule", label="显示名", router=router,
        template_dir=Path(__file__).parent / "templates",
        nav=[("入口", "/mymodule")]))

# service/app.py 的 _load_modules()
from mymodule import web as my_web
my_web.register_module()
```

模板里 `{% extends "base.html" %}`，`Jinja2Templates` 的 searchpath 已包含模块目录。
