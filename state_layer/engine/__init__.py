"""state_layer.engine —— 通用隐状态引擎。

对外只有一个函数：

    from state_layer.engine import fit_regime
    fit = fit_regime(features_df, k=4, seed=42)
    fit.states                     # 全样本状态编号
    fit.separation                 # Bhattacharyya 分离度
    fit.save("gold")               # saved_models/gold/hmm.pkl
    labels = fit.walk_forward(feat, labeler=my_labeler)   # 逐日因果标签

引擎不含任何资产语义：状态的"含义"由调用方的 labeler 决定。
"""

from state_layer.engine.api import RegimeFit, fit_regime
from state_layer.engine.bhattacharyya import pairwise as state_separation
from state_layer.engine.select import select_k

__all__ = ["fit_regime", "RegimeFit", "select_k", "state_separation"]
