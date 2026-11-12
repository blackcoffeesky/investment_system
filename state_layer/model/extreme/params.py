"""极端行情的规则参数（资产级可覆盖）。"""

from __future__ import annotations

from dataclasses import dataclass


EXTREME_UP = "extreme_up"
EXTREME_DOWN = "extreme_down"
EXTREME_VOL = "extreme_vol"
EXTREME_LABELS: tuple[str, ...] = (EXTREME_UP, EXTREME_DOWN, EXTREME_VOL)


@dataclass(frozen=True)
class ExtremeParams:
    enabled: bool = True
    ret_col: str = "log_return"
    vol_col: str = "realized_vol_20"
    sigma: float = 3.0          # |ret| > sigma × 滚动 σ
    sigma_window: int = 252
    pct: float = 0.99           # 波动率分位
    pct_window: int = 252
    abs_cap: float = 0.05       # 兜底：|ret| > 5%
    abs_cap_enabled: bool = False
    exit_days: int = 1          # 连续 N 日不触发才退出
    # 方向判定：|ret| 小于这个比例 × 触发阈值时，认为"方向不明" → EXTREME_VOL
    direction_ratio: float = 0.35

    def with_overrides(self, **kw) -> "ExtremeParams":
        return replace(self, **{k: v for k, v in kw.items() if k in self.__dataclass_fields__})

