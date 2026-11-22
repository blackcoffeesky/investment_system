"""模型契约：两个统计模型（Gaussian / Student-t）都要满足 `base.Model` 的约定。

链已退役（2026-11-22）：没有 `MODELS` 注册表、没有 `claim`/`remaining_index`。
这里测的是**模型层**的通用契约，与资产、分组无关。
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd

from state_layer.model import GaussianHMM, StudentTHMM

CASES = (
    (GaussianHMM, {"k": 3, "min_train": 200, "walk_forward": False}),
    (StudentTHMM, {"k": 3, "walk_forward": False}),
)


def _toy(n: int = 600, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2020-01-01", periods=n)
    return pd.DataFrame(
        {"log_return": rng.normal(0, 1, n) / 100,
         "realized_vol_20": rng.uniform(0.005, 0.03, n),
         "volume_z": rng.normal(0, 1, n)},
        index=idx)


def _labeler(means: pd.DataFrame) -> dict[int, str]:
    return {i: f"s{i}" for i in range(len(means))}


def test_roundtrip_is_lossless():
    """to_dict → from_dict 后，后验必须逐位相同。"""
    feat = _toy()
    for cls, params in CASES:
        model = cls(params)
        model.fit(feat, labeler=_labeler)
        data = model.to_dict()
        json.dumps(data)                      # 必须可 JSON 序列化

        clone = cls.from_dict(data)
        a = model.predict(feat).proba
        b = clone.predict(feat).proba
        assert a is not None and b is not None
        assert np.allclose(a.to_numpy(), b.to_numpy(), atol=1e-12), cls.__name__


def test_declares_state_params():
    """评价函数只吃 `state_params()`（模型不评价自己）。"""
    feat = _toy()
    for cls, params in CASES:
        model = cls(params)
        model.fit(feat, labeler=_labeler)
        p = model.state_params()
        assert p is not None, cls.__name__
        assert np.asarray(p["means"]).shape[0] == 3
        assert np.asarray(p["transmat"]).shape == (3, 3)


def test_valid_index_narrows_to_used_columns():
    """`valid_index` **只用于对齐**：只按自己用到的列判断非空。

    这是各组日期取交集的基础——模型不该被自己用不到的列的 NaN 挡住。
    """
    feat = _toy()
    feat.loc[feat.index[10], "volume_z"] = np.nan

    narrow = GaussianHMM({"k": 3, "features": ["log_return", "realized_vol_20"]})
    assert feat.index[10] in narrow.valid_index(feat)

    uses_volume = GaussianHMM({"k": 3, "features": ["volume_z"]})
    assert feat.index[10] not in uses_volume.valid_index(feat)


def test_fit_without_labeler_keeps_numeric_states():
    """不传 labeler 也能训练：状态是编号，`labels_` 为空（命名是资产的事）。"""
    feat = _toy()
    for cls, params in CASES:
        model = cls(params)
        model.fit(feat, labeler=None)
        assert model.states_ is not None and len(model.states_)
        assert model.labels_ is None
        assert model.states_.dtype.kind == "i"
