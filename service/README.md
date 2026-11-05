# service —— 服务层

> **更新日期：2026-11-03**

运维与展示层。**单进程**：一个 uvicorn，import 时加载各模块并注册路由。

---

## 启动

```bash
# 本机
uvicorn service.app:app --host 127.0.0.1 --port 8000

# 带鉴权（手机访问时用）
set SERVICE_TOKEN=your-token
uvicorn service.app:app --host 0.0.0.0 --port 8000
# 浏览器访问 http://<ip>:8000/?token=your-token
```

---

## 路由

| 路径 | 用途 |
|---|---|
| `/cards?asset=ashare` | 卡片列表（按日期倒序） |
| `/card/{asset}/{date}` | 单张卡片（`?kind=detail` 看详情） |
| `/ops` | 运维页：状态机 + 待确认消息 + 已静音清单 |
| `POST /task/confirm` | 确认消息（retry / skip + remind_at） |
| `POST /task/run` | 触发一轮（`mode=run` / `mode=rerun`）；**后台线程执行，立即返回** |
| `/health` | 自检：模块 + `journal_mode` + `busy_timeout` |

---

## 模块注册

模块把自己的路由、模板、导航项登记进 `service/registry.py`：

```python
# causual/web.py
def register_module() -> Module:
    return register(Module(
        name="causual", label="卡片", router=router,
        template_dir=TEMPLATES, nav=[("卡片", "/cards")]))
```

服务层只负责 `app.include_router(mod.router)`，并把自己的模板目录放在
Jinja2 searchpath 的最前面——模块模板可以 `{% extends "base.html" %}`。

---

## 运维状态机

```
fetch → manual_check → validate → generate_cards
```

- `python -m service.ops.manager --run` 跑一遍
- `--status` 看状态，`--ack <id> --action retry|skip` 确认消息
- `--reset <state>` 强制某状态回 pending

消息分级：

| 级别 | 行为 | 允许的操作 |
|---|---|---|
| ERROR | 阻塞 | 只能 RETRY（修好后重跑） |
| WARNING | 等确认 | RETRY / SKIP / ACK |
| INFO | 自动继续 | 仅 ACK（**不改状态**，且自动标记已读） |

`generate_cards`：数据层三状态全过后出卡。目标日期 = **真实数据最后一天**，
已存在则跳过（幂等）；单资产失败 WARNING（可静音），全失败 ERROR。

**静音**：SKIP 可带 `remind_at`：

| 值 | 含义 |
|---|---|
| `once` | 只跳过本次，明天照常提醒 |
| `days:N` | N 天内不再提醒 |
| `period_change` | 数据期变了才提醒（手工表默认） |

默认值按状态声明：`manual_check=period_change`、`validate=days:7`、`fetch=days:1`、
`generate_cards=days:1`。

**静音是逐条判断的**：`manual_check` 对每张逾期表单独发一条消息（key=表名），
静音 `cb_holdings` 不影响 `insider_net`。静音中的消息会自动标记为已读，不占待办。

**看门狗**：每次 `run()` 写一条 `ops_run_log`；`/ops` 若发现上次运行超过 26 小时，
顶部显示红色横幅；刚跑完会在页面顶部显示本轮摘要。

**运行 / 重跑**：点击后**立即返回**（真正的执行在后台线程），页面显示"正在运行…"并每 5 秒自动刷新——
fetch 可能跑好几分钟，不能占住 HTTP 请求。

`/ops` 的按钮会随状态切换——

| 状态组合 | 按钮 | 行为 |
|---|---|---|
| 有 `blocked` / `waiting_ack` | 禁用，提示先处理 | — |
| 全部 `done` / `snoozed` | **重跑** | `reset_all()` 后 `run()` |
| 还有 `pending` | **运行一次** | 直接 `run()` |

`reset_all()` 只重置 done/snoozed/pending，**不碰 blocked/waiting_ack**（否则等于绕过确认），
`attempts` 清零并把原值存进 `last_attempts`；**不清静音**。

---

## 文件

| 文件 | 职责 |
|---|---|
| `app.py` | FastAPI 实例、中间件（鉴权/日志/错误）、挂载、运维路由 |
| `registry.py` | 模块注册表、模板路径、导航项 |
| `data_service.py` | `DataService`：模块访问数据的唯一入口 |
| `templates/base.html` | 统一切片：导航 + 内容区 + 全局 CSS |
| `templates/ops.html` | 运维页 |
| `ops/` | 状态机（base_state / manager / messages / actions / states） |
