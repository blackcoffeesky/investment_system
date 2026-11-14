"""datahub —— 数据层。

只放共享常量。具体能力在三个子包里：

- `datahub.fin` 金融数据（读 / 更新 / 校验）
- `datahub.ops` 运维状态（状态机持久化）
- `datahub.core` 两者共用的基础设施（连接、DDL、异常）

**两个命名空间是隔离的**：`fin` 只认 metadata 里登记过的表；`ops` 只碰 `ops_*`。
"""

from __future__ import annotations

import os
from pathlib import Path

__version__ = "0.3.0"

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "datahub" / "datahub.sqlite"


def _load_env() -> None:
    """读仓库根的 `.env`（已在 gitignore 里），补进 os.environ。

    只补**还没设置**的键，不覆盖已有环境变量——服务器上可以用 systemd 注入。
    """
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()
