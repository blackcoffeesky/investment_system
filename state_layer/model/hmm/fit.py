"""HMM 的拟合与逐日解码（walk-forward）。

引擎不知道任何资产的语义——它只负责"把特征序列切成隐状态"，
以及"逐日因果地给出状态"（walk-forward）。把状态翻译成资产自己的标签，
由调用方传入 ``labeler`` 回调完成。

数值口径（**改这里会影响黄金的 0 格差异**）
------------------------------------------
- winsorize：训练窗口的 1% / 99% 分位（`shared.utils.data_processing.fit_bounds`）
- 标准化：`mean` / `std(ddof=0)`，`std==0` 时记 1（与 `StandardScaler` 一致）
- 拟合：`hmmlearn.GaussianHMM(covariance_type="full", n_iter=300, tol=1e-4, random_state=seed)`
- 解码：`predict`（Viterbi）/ `predict_proba`；walk-forward 里取**窗口最后一点**
  ——窗口以当日结尾，所以最后一点的后验就是过滤概率，不掺未来数据
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

from state_layer import metrics
from state_layer.model.base import to_numpy
from shared.utils.data_processing import fit_bounds, prep_features


Labeler = Callable[[pd.DataFrame], dict[int, object]]


def _num_eps() -> float:
    return float(np.finfo(float).eps) * 10.0


def _scaler_of(hist: pd.DataFrame, lo: pd.Series, hi: pd.Series) -> dict:
    """`StandardScaler` 的等价物（含零方差处理），但只交纯数组。"""
    clipped = hist.clip(lower=lo, upper=hi, axis=1)
    mean = clipped.mean().to_numpy(dtype=float)
    scale = clipped.std(ddof=0).to_numpy(dtype=float)
    scale = np.where(scale < _num_eps(), 1.0, scale)
    return {"mean": mean, "scale": scale}


def _fit_gauss(X: np.ndarray, n_states: int, seed: int):
    from hmmlearn.hmm import GaussianHMM

    model = GaussianHMM(n_components=n_states, covariance_type="full",
                        n_iter=300, random_state=seed, tol=1e-4)
    with np.errstate(all="ignore"):
        model.fit(X)
    return model


def _raw_means(model, scaler: dict, columns) -> pd.DataFrame:
    raw = model.means_ * np.asarray(scaler["scale"]) + np.asarray(scaler["mean"])
    return pd.DataFrame(raw, columns=list(columns))


@dataclass
class RegimeFit:
    """一次拟合的完整结果。状态编号只在引擎内部有意义。"""

    k: int
    features: list[str]
    states: pd.Series                      # 全样本 Viterbi 状态编号
    proba: pd.DataFrame                    # 后验概率
    means: np.ndarray                      # 标准化空间的状态均值
    covars: np.ndarray
    transmat: np.ndarray
    startprob: np.ndarray
    scaler_mean: np.ndarray
    scaler_scale: np.ndarray
    clip_lo: pd.Series
    clip_hi: pd.Series
    seed: int = 42
    separation: dict = field(default_factory=dict)

    # ---------------------------------------------------------------- 派生
    def state_params(self) -> dict:
        """纯数组（供 `metrics` 用）。模型不评价自己。"""
        return {"means": self.means, "covars": self.covars,
                "transmat": self.transmat, "startprob": self.startprob}

    def raw_means(self) -> pd.DataFrame:
        """还原到原始量纲的状态均值（资产标签器用这个判断方向/波动）。"""
        raw = self.means * self.scaler_scale + self.scaler_mean
        return pd.DataFrame(raw, columns=self.features)

    def spec(self) -> dict:
        return to_numpy({
            "schema": "regime-v2",
            "k": self.k,
            "features": list(self.features),
            "means": self.means,
            "covars": self.covars,
            "transmat": self.transmat,
            "startprob": self.startprob,
            "scaler_mean": self.scaler_mean,
            "scaler_scale": self.scaler_scale,
            "clip_lo": self.clip_lo,
            "clip_hi": self.clip_hi,
            "seed": self.seed,
            "min_bhattacharyya": self.separation.get("min", 0.0),
        })

    def window_dict(self) -> dict:
        """最后窗口的纯数据（在线算后验用）。字段与 walk_forward 的 last 一致。"""
        return to_numpy({
            "means": self.means,
            "covars": self.covars,
            "transmat": self.transmat,
            "startprob": self.startprob,
            "scaler_mean": self.scaler_mean,
            "scaler_scale": self.scaler_scale,
            "clip_lo": self.clip_lo.to_numpy(dtype=float),
            "clip_hi": self.clip_hi.to_numpy(dtype=float),
            "features": list(self.features),
            "state_labels": {},
        })

    @classmethod
    def from_spec(cls, spec: dict) -> "RegimeFit":
        """从 spec 字典重建（不重跑拟合），用于加载模型。"""
        return cls(
            k=int(spec["k"]),
            features=list(spec.get("features", [])),
            states=pd.Series(dtype="int64"),
            proba=pd.DataFrame(),
            means=np.asarray(spec["means"], dtype=float),
            covars=np.asarray(spec["covars"], dtype=float),
            transmat=np.asarray(spec["transmat"], dtype=float),
            startprob=np.asarray(spec["startprob"], dtype=float),
            scaler_mean=np.asarray(spec["scaler_mean"], dtype=float),
            scaler_scale=np.asarray(spec["scaler_scale"], dtype=float),
            clip_lo=pd.Series(spec["clip_lo"], index=list(spec.get("features", []))),
            clip_hi=pd.Series(spec["clip_hi"], index=list(spec.get("features", []))),
            seed=int(spec.get("seed", 42)),
            separation={"min": spec.get("min_bhattacharyya", 0.0)},
        )

    # ---------------------------------------------------------------- 逐日解码
    def walk_forward(
        self,
        feat: pd.DataFrame,
        labeler: Labeler,
        min_train: int = 500,
        refit_every: int = 60,
        window: int = 250,
    ) -> tuple[pd.Series, pd.DataFrame, dict]:
        """逐日给出标签：每天只用当日及之前的数据重估与解码。

        ``labeler`` 接收**该窗口的原始状态均值**（DataFrame），返回 {状态号: 标签}。
        """
        valid = feat.dropna()
        if len(valid) < min_train:
            raise ValueError(f"有效样本 {len(valid)} < min_train={min_train}")

        out = pd.Series("", index=feat.index, dtype=object)
        proba = pd.DataFrame(np.nan, index=valid.index, columns=list(range(self.k)))
        model = None
        scaler: dict | None = None
        lo = hi = None
        label_map: dict[int, object] = {}

        for i in range(min_train, len(valid)):
            if model is None or (i - min_train) % refit_every == 0:
                cutoff = valid.index[i]
                hist = feat.loc[:cutoff].dropna()
                lo, hi = fit_bounds(hist)
                scaler = _scaler_of(hist, lo, hi)
                Xtr = prep_features(hist, scaler, lo, hi)
                model = _fit_gauss(Xtr, self.k, seed=self.seed)
                label_map = labeler(_raw_means(model, scaler, feat.columns))

            tail = feat.loc[: valid.index[i]].dropna().tail(window)
            X = prep_features(tail, scaler, lo, hi)
            post = np.asarray(model.predict_proba(X)[-1], dtype=float)
            proba.loc[valid.index[i]] = post
            out.loc[valid.index[i]] = label_map[int(np.argmax(post))]

        last = {
            "means": model.means_,
            "covars": model.covars_,
            "transmat": model.transmat_,
            "startprob": model.startprob_,
            "scaler_mean": scaler["mean"],
            "scaler_scale": scaler["scale"],
            "clip_lo": lo.to_numpy(dtype=float),
            "clip_hi": hi.to_numpy(dtype=float),
            "features": list(feat.columns),
            "state_labels": {int(k_): str(v) for k_, v in label_map.items()},
        }
        return out, proba, last


def fit_regime(
    features_df: pd.DataFrame,
    k: int,
    seed: int = 42,
    bh_threshold: float = 0.4,
) -> RegimeFit:
    """在整段特征上拟合一个 HMM。"""
    feat = features_df.dropna()
    if len(feat) < 100:
        raise ValueError(f"有效样本只有 {len(feat)}，无法拟合")

    lo, hi = fit_bounds(feat)
    scaler = _scaler_of(feat, lo, hi)
    X = prep_features(feat, scaler, lo, hi)

    model = _fit_gauss(X, k, seed=seed)
    states = pd.Series(model.predict(X), index=feat.index, name="state")
    proba = pd.DataFrame(model.predict_proba(X), index=feat.index,
                         columns=[f"p{i}" for i in range(k)])
    sep = metrics.score("bhattacharyya", {"means": model.means_,
                                          "covars": model.covars_},
                        threshold=bh_threshold)

    return RegimeFit(
        k=k,
        features=list(features_df.columns),
        states=states,
        proba=proba,
        means=model.means_,
        covars=model.covars_,
        transmat=model.transmat_,
        startprob=model.startprob_,
        scaler_mean=scaler["mean"],
        scaler_scale=scaler["scale"],
        clip_lo=lo,
        clip_hi=hi,
        seed=seed,
        separation=sep,
    )
