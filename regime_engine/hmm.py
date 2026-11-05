"""HMM 训练、解码、平滑。只处理数值，不知道任何资产语义。"""

from __future__ import annotations

import numpy as np
import pandas as pd
from hmmlearn.hmm import GaussianHMM
from sklearn.preprocessing import StandardScaler


def clip_frame(mat: pd.DataFrame, lo: pd.Series, hi: pd.Series) -> pd.DataFrame:
    """按列上下界 winsorize（避免用未来分位数，边界由调用方按训练窗口给出）。"""
    return mat.clip(lower=lo, upper=hi, axis=1)


def fit_window_bounds(feat: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """用给定窗口的分位数确定 winsorize 边界（只应传入截止日及之前的数据）。"""
    hist = feat.dropna()
    if hist.empty:
        raise ValueError("训练窗口内没有有效特征行")
    return hist.quantile(0.01), hist.quantile(0.99)


def fit(
    X: np.ndarray,
    n_states: int,
    seed: int = 42,
    n_iter: int = 300,
    tol: float = 1e-4,
) -> GaussianHMM:
    hmm = GaussianHMM(
        n_components=n_states,
        covariance_type="full",
        n_iter=n_iter,
        random_state=seed,
        tol=tol,
    )
    with np.errstate(all="ignore"):
        hmm.fit(X)
    return hmm


def decode(hmm: GaussianHMM, X: np.ndarray) -> np.ndarray:
    """Viterbi 状态序列。"""
    return hmm.predict(X)


def smoothed_proba(hmm: GaussianHMM, X: np.ndarray) -> np.ndarray:
    """后验状态概率（前向-后向平滑）。"""
    return hmm.predict_proba(X)


def make_scaler(X: np.ndarray) -> StandardScaler:
    return StandardScaler().fit(X)
