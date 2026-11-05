"""datahub —— 数据层。

只放共享常量。具体能力在三个子包里：

- `datahub.fin` 金融数据（读 / 更新 / 校验）
- `datahub.ops` 运维状态（状态机持久化）
- `datahub.core` 两者共用的基础设施（连接、DDL、异常）

**两个命名空间是隔离的**：`fin` 只认 metadata 里登记过的表；`ops` 只碰 `ops_*`。
"""

from __future__ import annotations

from pathlib import Path

__version__ = "0.3.0"

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "datahub" / "datahub.sqlite"
