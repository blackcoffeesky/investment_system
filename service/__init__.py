"""service —— 服务层。

两层职责：

1. **统一入口**：所有模块通过 `DataService` 访问数据，不再直接 `import datahub`
2. **运维状态机**：`service.ops` 负责调度各模块的日常运行、消息确认、断点恢复

单进程模型：一个 uvicorn 进程，import 时加载各模块并注册路由。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
