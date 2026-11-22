"""资产层抽象：`GroupResult`（一组状态的结果）+ `Estimator`（资产装配协议）。

判据句（lead 2026-11-22 定的，后面所有设计都按它判）
----------------------------------------------------
**模型层不该知道有组。**

- `model/base.py` 抽象**统计模型**（Gaussian / Student-t / …）：它只认"特征矩阵 + K"，
  不知道资产、不知道组。
- 这里抽象**资产装配**：这个资产用哪些模型、喂什么特征、产出几组状态。
  组不是类的属性，是 `fit()` / `predict()` 的**运行期产物**（返回 dict 的 key）；
  所以协议里**没有** `groups: tuple[str, ...]` 这种东西。

`primary`（哪个组给下游用）不放这里，放 `state_assets/<asset>/config.py`——它和 `K`、
特征清单同级，属资产配置。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

#: 落盘长表的列（`store.save_estimate(groups=...)` 认这个形状）
GROUP_COLUMNS = ("date", "group", "state", "label", "p_max")


@dataclass
class GroupResult:
    """一个状态分组的完整结果。**组名由资产自己定**（ASCII，稳定，落盘用）。"""

    name: str                       # short_price / short_vol / long_price / raw
    title: str                      # 展示名：短价格 / 短波动 / 长价格 / 原始状态
    k: int
    states: pd.Series               # index=date → int 0..k-1
    labels: dict[int, str]          # 编号 → 稳定标签（随产物落盘，重训也要能对上）
    proba: pd.DataFrame | None = None
    features: tuple[str, ...] = ()
    profile: pd.DataFrame = field(default_factory=pd.DataFrame)
    usable: bool = True
    fit_id: str = ""
    window: dict | None = None      # 在线后推：参数 + scaler + clip + labels
    # 逐日**权威**标签。walk-forward 每个窗口的命名映射可能不同，`labels` 记不下
    # 这种"同一天用当时那份映射"的结果；模型自带 `labels_` 时就放这里。
    label_values: pd.Series | None = None
    # 这组的特征需要多少**交易日**历史才开始非空（特征预热，不是取数预热）。
    # 装配点据此把 `get_data(start=…)` 往前推，否则短窗口头部整段是 NaN。
    warmup_bars: int = 0

    def label_series(self) -> pd.Series:
        """逐日字符串标签。`label_values` 是权威（walk-forward 逐窗映射）；否则按编号映。"""
        if self.label_values is not None and len(self.label_values):
            s = self.label_values.reindex(self.states.index)
            return pd.Series(s.to_numpy(), index=self.states.index,
                             name="label", dtype=object)
        mapped = self.states.map(lambda s: self.labels.get(int(s), str(s)))
        return pd.Series(mapped.to_numpy(), index=self.states.index,
                         name="label", dtype=object)

    def rows(self) -> pd.DataFrame:
        """→ 落盘长表的行（`GROUP_COLUMNS`）。"""
        frame = pd.DataFrame({
            "date": pd.DatetimeIndex(self.states.index),
            "group": self.name,
            "state": self.states.to_numpy(),
            "label": self.label_series().to_numpy(),
        })
        if self.proba is not None and len(self.proba):
            p_max = self.proba.max(axis=1).reindex(self.states.index)
            frame["p_max"] = p_max.to_numpy()
        else:
            frame["p_max"] = None
        return frame


def label_fingerprint(labels: pd.Series) -> str:
    """**标签指纹**：一次拟合产出的逐日标签（日期 + 值）的 sha1 前 12 位。

    契约（用户 2026-11-22 定的硬约束）
    --------------------------------
    `label_signature` **只能**由 **primary 组**的标签序列决定，不掺：
    组名列表、组数、其他组的标签、`primary` 字段本身、特征清单。

    因此"A 股加 short_vol / long_price"**不会**改变签名 → causual 的边快照不失效。
    想改签名，只有一条路：primary 组的标签本身变了（重拟合换了标签体系）。
    """
    import hashlib

    s = labels.dropna().astype(str)
    raw = "\n".join(f"{d:%Y-%m-%d}:{v}" for d, v in s.items())
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def needed_bars(groups) -> int:
    """这组结果需要多少交易日预热 = 各组 `warmup_bars` 的最大值。

    装配点（`fit.py` / `web/routes.py`）用它把取数起点往前推：短价格 10、
    短波动 5、长价格 120 → 120。传 `{组名: GroupResult}` 或单个 `GroupResult` 都行。
    """
    if isinstance(groups, GroupResult):
        return int(groups.warmup_bars)
    return max((int(g.warmup_bars) for g in groups.values()), default=0)


def to_long_df(groups: dict[str, GroupResult]) -> pd.DataFrame:
    """多组 → 一张长表。组数可变，所以用长表而不是宽表。"""
    frames = [g.rows() for g in groups.values()]
    if not frames:
        return pd.DataFrame(columns=list(GROUP_COLUMNS))
    return pd.concat(frames, ignore_index=True)


def make_fit_id(asset: str, group: str, k: int, features, labels,
                fit_date: str) -> str:
    """一次拟合的指纹：落盘进 spec，重训后对不上就说明产物过期了。"""
    import hashlib

    raw = "|".join([asset, group, str(k), ",".join(features),
                    ",".join(f"{a}:{b}" for a, b in sorted(labels.items())),
                    fit_date])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


class Estimator(ABC):
    """资产装配协议：**组清单不在协议里**（组是运行期产物）。

    实现方（`state_assets/<asset>/estimator.py`）负责：
    1. 把 panel 变成该资产需要的特征（内部调自己的 `features.py`）；
    2. 选模型、定 K、决定出几组；
    3. 返回 `{组名: GroupResult}`，并把 `labels` 映射一起交出来（落盘 + 进签名）。

    不负责：取数（`get_data` 只在 `cli.py` / `web/routes.py` / service）、落盘（`store`）、
    页面组合（`analysis/combine.py`）。
    """

    asset: str

    def __init__(self, params: dict[str, Any] | None = None):
        self.params: dict[str, Any] = dict(params or {})

    @classmethod
    @abstractmethod
    def load(cls, asset: str) -> "Estimator":
        """从 `store` 重建（不重训）。缺产物时抛 `FileNotFoundError`。"""

    @abstractmethod
    def fit(self, panel: pd.DataFrame) -> dict[str, GroupResult]:
        """训练并返回 `{组名: GroupResult}`。"""

    @abstractmethod
    def predict(self, panel: pd.DataFrame) -> dict[str, GroupResult]:
        """只推断（states / proba），不重训。"""

    @abstractmethod
    def spec(self) -> dict:
        """落盘用元信息：`primary` / 各组 k·features·labels·fit_id·usable·signature`。"""

    @abstractmethod
    def to_params(self) -> dict:        """落盘用的模型参数：`{groups: {组名: {"model": 可序列化 dict, "window": …}}}`。

        为什么不直接交模型对象：`store` 只认"能 pickle 的纯数据"，
        模型对象的结构随库版本变（见 `model/hmm/params.py` 的说明）。
        """

    # ---------------- 装配点用的元信息
    @property
    def warmup_bars(self) -> int:
        """这个资产需要多少交易日特征历史（各组最大值）。默认 0 = 不需要预热。"""
        return 0
