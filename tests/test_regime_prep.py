"""验收：特征预处理只有一条路径。

v5.8 的 bug 就是两条路径各写一遍 clip+standardize，顺序还不一样。
现在 fit / walk_forward / 在线后验都调 `prep_features`，这里断言
"用同一条路径重算最后一天的后验" 与 fit 时存下的 `last_proba` 一致。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from shared.data_processing import prep_features


def _toy_features(n: int = 400) -> pd.DataFrame:
    rng = np.random.default_rng(0)
    idx = pd.date_range("2020-01-01", periods=n, freq="B")
    return pd.DataFrame(
        {
            "log_return": rng.normal(0, 0.01, n),
            "realized_vol_20": rng.uniform(0.005, 0.03, n),
            "volume_z": rng.normal(0, 1, n),
        },
        index=idx,
    )


def test_prep_clips_before_standardizing():
    """先裁剪后标准化 = 把原始值夹到 [lo, hi] 再减均值除标准差。"""
    feat = _toy_features()
    lo = feat.quantile(0.01)
    hi = feat.quantile(0.99)

    from sklearn.preprocessing import StandardScaler

    scaler = StandardScaler().fit(feat.clip(lower=lo, upper=hi, axis=1))
    got = prep_features(feat, scaler, lo, hi)

    manual = (feat.clip(lower=lo, upper=hi, axis=1).to_numpy() - scaler.mean_) / scaler.scale_
    assert np.allclose(got, manual)

    # 反例：先标准化再按原始分位裁剪，绝大多数点会被压到边界
    wrong = np.clip((feat.to_numpy() - scaler.mean_) / scaler.scale_,
                    lo.to_numpy(), hi.to_numpy())
    assert not np.allclose(got, wrong)


def test_prep_accepts_dict_scaler():
    """在线重建时只有 mean/scale 两个数组，也要走同一条路径。"""
    feat = _toy_features()
    lo = feat.quantile(0.01)
    hi = feat.quantile(0.99)
    mean = np.zeros(feat.shape[1])
    scale = np.ones(feat.shape[1])
    got = prep_features(feat, {"mean": mean, "scale": scale}, lo, hi)
    assert np.allclose(got, feat.clip(lower=lo, upper=hi, axis=1).to_numpy())


def test_fit_and_recompute_agree():
    """fit 之后用在线路径重算最后一天后验，必须与存储值一致。"""
    from state_layer.model.hmm import fit as RE
    from state_layer.model.hmm import serialize as S
    from state_layer.model.hmm import _ops as H

    feat = _toy_features(600)
    fit = RE.fit_regime(feat, k=3, seed=0)
    labels, proba, last = fit.walk_forward(feat, labeler=lambda m: {i: f"s{i}" for i in range(len(m))},
                                           min_train=200, refit_every=100)
    spec = {**S.to_numpy(last), "fit_id": "x"}
    pick = feat.loc[: labels[labels != ""].index[-1]]
    lo, hi = H.fit_window_bounds(pick)
    X = prep_features(pick, {"mean": spec["scaler_mean"], "scale": spec["scaler_scale"]}, lo, hi)
    p = S.rebuild(spec).predict_proba(X)[-1]
    assert np.allclose(p, proba.iloc[-1].to_numpy(), atol=1e-9)
