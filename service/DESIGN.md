# service 设计文档

> **更新日期：2026-11-14**　｜　使用说明见 [`README.md`](README.md)　｜　项目总览见 [`../README.md`](../README.md)

---

## 1. 定位

服务层做两件事：

1. **装配与框架**：把各模块的页面挂上来、把模块能力收成端口、提供鉴权与错误处理。
2. **运维**：状态机保证"抓数 → 检查 → 出卡"这条链路每天走完，并把结果摊在网页上。

它**不含业务逻辑**——因果计算在 `causual`，取数在 `datahub`。

---

## 2. 模块模型（重要）

一个模块 = **核心代码 + `web/` + `port.py`**：

```
datahub/        web/(数据页)   port.py(DataPort)      fin/ ops/
state_layer/    web/(/state)   port.py(StatePort)     model/ state_assets/ analysis/
causual/        web/(卡片·拟合) port.py(CausalPort)    causual_core/ causual_assets/
service/        app.py registry.py ops/(状态机) templates/(共享骨架)
```

四个模块都遵循同一个模型：**核心代码 + `web/` + `port.py`**。
`shared/` 是跨模块纯工具，不算模块。

**注册即共享**：模块在 `register()` 时声明 `service_port_factory`，service 启动时会把它
收进 `ServiceBag`。此后任何模块都能通过框架拿到它——不需要互相 import。

### 取用规则

| 场景 | 允许的方式 |
|---|---|
| **service 内部**（状态机、app、路由） | 必须走 `service_port_factory`：`ctx.services["datahub"]`、`request.app.state.services["causual"]` |
| **模块内部**（causual 想用 datahub） | 可以直接 `from datahub.port import get_port` |
| **任何地方** | ❌ 不许 import 别的模块的内部实现（`datahub.fin.read` 之类） |

命令行入口是例外中的例外：`causual/causual_core/cli.py` 可以直接用 `datahub.port`，
因为它不在 service 里跑，没有 `Context`。

**唯一的跨层约定**：模块把 `<module>/port.py` 当作自己对外的全部脸面；
port 里没有的东西，别人就不该用。

---

## 3. 进程模型

**单进程、单 uvicorn**。模块在 import 时注册，`app.py` 负责挂载。

```
uvicorn service.app:app
   │
   ├── import 时：_load_modules() → registry.load() → 各模块 register_module()
   ├── 启动时：_startup_recover()  ← 把上次崩在 running 的状态折算成 blocked
   └── 请求时：各路由
```

状态机跑在**后台线程**（`/task/run` 立刻返回），否则一次 fetch 几分钟，HTTP 请求会一直挂着。
`RUN_LOCK` 保证同一时刻只有一个"会改状态"的操作。

---

## 4. 文件职责

### 4.1 顶层

| 文件 | 职责 |
|---|---|
| `app.py` | FastAPI 实例；鉴权/日志/错误中间件；`/ops`、`/task/*`、`/health`；挂载各模块路由 |
| `registry.py` | `Module` 数据类、`register()`、`load()`、`ports()`、模板路径、导航项 |
| `card_service.py` | ❌ 已删除——卡片能力归 `causual/port.py` |
| `data_service.py` | ❌ 已删除——数据能力归 `datahub/port.py` |
| `templates/base.html` | 全站骨架（所有模块共用） |
| `templates/ops.html` | 运维页 |
| `templates/error.html` | 错误页 |

### 4.2 `ops/` —— 状态机

| 文件 | 职责 |
|---|---|
| `base_state.py` | `BaseState` ABC：`name / prefix / label / default_snooze` |
| `messages.py` | `MessageLevel` / `StateStatus` / `PASSABLE` / `Message` / `StateResult` |
| `actions.py` | `AckAction` + `ALLOWED_ACTIONS` |
| `context.py` | `ServiceBag`（缺端口即报错）+ `Context`——**只有状态机用** |
| `manager.py` | 编排：`run()`、`ack()`、`reset/reset_all()`、静音判定、运行日志 |
| `states/data.py` | `FetchState` / `ValidateState`（手工表 + 缺口，已合并） |
| `states/causal.py` | `GenerateCardsState` / `GenerateMacroState` |
| `states/__init__.py` | `STATES` 列表——**加状态只改这一个文件** |

---

## 5. 状态机

```
fetch → validate → generate_cards → generate_macro
```

```python
class XxxState(BaseState):
    name = "xxx"
    prefix = ("fetch",)             # 前置状态全部 passable 才跑
    label = "显示名"
    default_snooze = "days:7"

    def run(self, ctx) -> StateResult:
        data = ctx.data                          # = ctx.services["datahub"]
        cards = ctx.services["causual"]
        ...
```

| 情况 | 状态 | 消息 |
|---|---|---|
| 数据源不可用但仍在保鲜期 | `waiting_ack` | WARNING（可跳过） |
| 结构性错误 | `blocked` | ERROR（只能重跑） |
| 进程崩溃留下 `running` | `blocked` | ERROR `UNEXPECTED_INTERRUPT`，**不自动重跑** |
| 告警在静音期 | `snoozed` | 正常执行，只是不打断 |

---

## 6. 数据怎么流到 causual

service **不把端口塞给模块**，而是读好数据再传：

```python
series = {t: data.read(t) for t in cards.required_tables(asset)}   # 多列表再按列拆
cards.generate(asset, series, data.metadata(), date=date)
```

causual 因此不知道数据从哪来——CLI 传库里的数据，service 传端口读出的数据，
测试传假的 Series。这也是为什么 `causual_assets/common.py` 里没有任何数据层 import。

---

## 7. HTTP 接口

| 路径 | 方法 | 说明 |
|---|---|---|
| `/cards` | GET | 卡片列表（module: causual） |
| `/card/{asset}/{date}` | GET | 单张卡 |
| `/card/{asset}/{date}/regenerate` | POST | 重新生成 |
| `/fit` | GET / POST | 拟合页（module: causual） |
| `/data` | GET | 数据表清单 + 上传 + 单值录入（module: datahub） |
| `/data/upload`、`/data/manual` | POST | 手工数据入库 |
| `/ops` | GET | 运维页（service 自己） |
| `/task/run` | POST | 触发一轮（后台线程） |
| `/task/confirm` | POST | 确认消息，确认后**自动续跑** |
| `/health` | GET | `{ok, modules, ports, journal_mode, busy_timeout}` |

---

## 8. 关键约束

1. **模块不 import 别的模块的内部实现**；对外只用 `<module>/port.py`
2. **service 内部必须通过 `ServiceBag` 拿端口**，不许 `from datahub.fin import ...`
3. **页面加载不写库**：`Manager.__init__` 只读快照，恢复只在启动和 `run()`
4. **长任务不上 HTTP 线程**：起后台线程，页面轮询 `ops_run_log`
5. **消息结构不动**：展示层可按 `(state, level)` 分组折叠，但 `Message` 仍是 `(key, period)` 粒度
6. **连接必须走 `core.conn.connect()`**：裸 `sqlite3.connect` 只有 5 秒超时

---

## 9. 类型检查

`pyrightconfig.json` 只检查**接口层**（app / registry / ops / port / web / write）。

```bash
pyright     # 当前 0 error
```

pandas 密集的数值代码不查——那里的报错 90% 是 stub 噪声，而真正的 bug 都出在
"签名改了、调用点没改"（已踩过三次）。把检查范围收在接口层，噪音小、收益高。

---

## 10. 扩展新模块

```python
# mymodule/web/routes.py
def register_module():
    from mymodule.port import get_port

    return register(Module(
        name="mymodule", label="显示名", router=router,
        template_dir=Path(__file__).parent / "templates",
        nav=[("入口", "/mymodule")],
        service_port_factory=get_port))     # 有对外能力就写，没有就省略

# service/registry.py 的 load()
from mymodule import web as my_web
my_web.register_module()
```

模板里 `{% extends "base.html" %}`，Jinja2 的 searchpath 已包含模块目录。
