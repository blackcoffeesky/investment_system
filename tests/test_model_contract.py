"""模型契约测试：序列化必须无损。"""

import numpy as np
import pandas as pd

from state_layer.model.hmm import GaussianHMM


def _toy(n=600, seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2020-01-01", periods=n)
    return pd.DataFrame(
        {"log_return": rng.normal(0, 1, n),
         "realized_vol_20": rng.normal(0, 1, n),
         "volume_z": rng.normal(0, 1, n)},
        index=idx)


def test_model_roundtrip_is_lossless():
    """to_dict → from_dict 后，后验必须逐位相同。"""
    feat = _toy()
    model = GaussianHMM().fit(feat, k=3, seed=7)
    data = model.to_dict()

    import json

    json.dumps(data)          # 必须可 JSON 序列化

    clone = GaussianHMM.from_dict(data)
    a = model.infer(feat)["proba"].to_numpy()
    b = clone.infer(feat)["proba"].to_numpy()
    assert np.allclose(a, b, atol=1e-12)


def test_model_declares_state_params():
    """评价函数只吃 state_params()，模型自己不评价。"""
    feat = _toy()
    model = GaussianHMM().fit(feat, k=3, seed=7)
    params = model.state_params()
    for k in ("means", "covars", "transmat"):
        assert k in params
    assert np.asarray(params["means"]).shape[0] == 3
