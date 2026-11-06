"""kernel —— 模块与框架共同依赖的最小层。

依赖方向：

    模块（datahub / causual / …） ──→ kernel ←── service

左边不认识右边，右边也不认识左边。模块对外暴露什么能力，由它自己
声明 `service_port_factory`；service 只负责装配。

| 文件 | 职责 |
|---|---|
| `registry.py`  | `Module` 数据类、`register()`、模块枚举、模板路径、导航项 |
| `context.py`   | `ServiceBag` + `Context`：状态运行期拿到的东西 |
| `protocols.py` | 模块对外能力的**类型契约**（`DataPort` / `CausalPort`） |
"""

from kernel.registry import Module, all_modules, nav_items, ports, register, template_dirs

__all__ = ["Module", "register", "all_modules", "template_dirs", "nav_items", "ports"]
