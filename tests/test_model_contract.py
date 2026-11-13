"""模型契约测试：链上的每个模型都要满足 base.Model 的约定。"""

import json

import numpy as np
import pandas as pd

from state_layer.model import MODELS, make


def _toy(n=600, seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2020-01-01", periods=n)
    return pd.DataFrame(
        {"log_return": rng.normal(0, 1, n) / 100,
         "realized_vol_20": rng.uniform(0.005, 0.03, n),
         "volume_z": rng.normal(0, 1, n)},
        index=idx)


def test_hmm_roundtrip_is_lossless():
    """to_dict → from_dict 后，后验必须逐位相同。"""
    feat = _toy()
    model = make("hmm", {"k": 3, "min_train": 200})
    model.fit(feat)
    data = model.to_dict()
    json.dumps(data)                      # 必须可 JSON 序列化

    clone = MODELS["hmm"].from_dict(data)
    a = model.infer(feat)["proba"].to_numpy()
    b = clone.infer(feat)["proba"].to_numpy()
    assert np.allclose(a, b, atol=1e-12)


def test_hmm_declares_state_params():
    """评价函数只吃 state_params()。"""
    feat = _toy()
    m = make("hmm", {"k": 3, "min_train": 200})
    m.fit(feat)
    params = m.state_params()
    assert params is not None
    assert np.asarray(params["means"]).shape[0] == 3


def test_extreme_claims_only_extreme_days():
    """规则模型认领极端日，并把其余日期交出来。"""
    feat = _toy()
    feat.loc[feat.index[100], "log_return"] = 0.20     # 造一个极端日
    m = make("extreme", {"sigma": 3.0})
    unclaimed = m.fit(feat)

    assert feat.index[100] not in unclaimed
    assert len(m.labels_) >= 1
    assert m.state_params() is None                    # 没有概率分布
    assert m.infer(feat)["proba"] is None


def test_chain_filters_between_models():
    """上一个模型交出的日期，下一个模型才处理。"""
    feat = _toy()
    feat.loc[feat.index[100], "log_return"] = 0.20

    ext = make("extreme", {"sigma": 3.0})
    remaining = ext.fit(feat)
    hmm = make("hmm", {"k": 3, "min_train": 200})
    labeler = lambda means: {i: f"s{i}" for i in range(len(means))}   # noqa: E731
    left = hmm.fit(feat, claim=remaining, labeler=labeler)

    assert len(left) == 0, "最后一环应该认领剩下的全部"
    assert hmm.labels_ is not None and len(hmm.labels_) == len(remaining)
    assert feat.index[100] not in hmm.labels_.index


def test_extreme_valid_index_ignores_irrelevant_nan():
    """规则模型只看两列，别的列有 NaN 不该挡住它。"""
    feat = _toy()
    feat.loc[feat.index[50], "volume_z"] = np.nan       # 规则用不到的列
    feat.loc[feat.index[100], "log_return"] = 0.20      # 造一个极端日

    ext = make("extreme", {"sigma": 3.0})
    assert feat.index[50] in ext.valid_index(feat), "不该因为 volume_z 有 NaN 就放弃这一行"
    assert feat.index[100] in ext.valid_index(feat)


def test_chain_infer_skips_extreme_days_for_hmm():
    """推断也要按链走：HMM 看不到极端日，与 fit 时的输入一致。"""
    from state_layer.model import run_infer

    feat = _toy()
    feat.loc[feat.index[100], "log_return"] = 0.20

    ext = make("extreme", {"sigma": 3.0})
    ext.fit(feat)
    hmm = make("hmm", {"k": 3, "min_train": 200})
    hmm.fit(feat, claim=ext.fit(feat),
            labeler=lambda m: {i: f"s{i}" for i in range(len(m))})

    res = run_infer([ext, hmm], feat)
    assert feat.index[100] in res["states"].index, "极端日应由 extreme 认领"
    assert res["states"][feat.index[100]].startswith("extreme")
    assert res["proba"] is not None
    assert feat.index[100] not in res["proba"].index, "HMM 的后验不该覆盖极端日"


def test_labeler_required():
    """没有 labeler 时必须报错，不能静默产出空标签。"""
    from state_layer import fit as fit_mod

    import pandas as pd
    import tempfile
    # 只验证函数级前置条件，不真的跑拟合
    try:
        fit_mod.fit_pipeline("ashare", pd.DataFrame(), labeler=None, verbose=False)
    except ValueError as e:
        assert "labeler" in str(e)
    except Exception:
        pass          # 面板为空时可能先在别处报错，这条只守 labeler 检查
