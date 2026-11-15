"""兼容 shim：实现已移到 `shared.utils.data_processing`。

2026-11-14 起 `shared/` 拆成 `utils/`（纯工具）+ `adapter/`（取数耦合层）。
这里保留旧路径转发；新代码请用 `from shared.utils.data_processing import ...`。
"""

from shared.utils.data_processing import (edge_z, fit_bounds, periodic_z,
                                          prep_features)

__all__ = ["prep_features", "fit_bounds", "periodic_z", "edge_z"]
