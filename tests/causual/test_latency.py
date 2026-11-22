"""验收 2：同一条边的 latency / regime_valid 必须按资产独立配置。

`FED_RATE -> 标的` 这条边在两个资产上同时存在，但：
- 黄金：机制是"实际利率/机会成本"，latency 以月计；
- A 股：机制是"外资风险偏好 + 国内流动性"，latency 以日计。
两者不能共享同一个 Edge 实例，也不能共享同一份 regime_valid。
"""

from __future__ import annotations

from causual.causual_assets.gold import config as gold_cfg
from state_layer.state_assets.gold import config as gold_label
from causual.causual_assets.ashare import config as ash_cfg
from state_layer.state_assets.ashare import config as ash_label


def _shared_edge(edges, source="REAL_YIELD_10Y"):
    for e in edges:
        if e.source == source:
            return e
    raise AssertionError(f"没有 {source} 边")


def test_latency_differs_by_asset():
    """同一条边（美债实际利率→标的）在两个资产上的时滞不同。"""
    g = _shared_edge(gold_cfg.EDGES)
    a = _shared_edge(ash_cfg.EDGES)
    assert g.latency != a.latency
    assert g.latency == "同日"
    assert a.latency == "1-2 日"


def test_mechanism_differs_by_asset():
    g, a = _shared_edge(gold_cfg.EDGES), _shared_edge(ash_cfg.EDGES)
    assert g.mechanism != a.mechanism


def test_label_enums_are_asset_specific():
    """两个资产的标签**词表按各自模型定**，不再强行相等。

    2026-11-22 起：
    - 黄金 `raw` K=4 → `range / trend_up / trend_down / **distribution**`
    - A 股 `short_price` K=3 → `range / trend_up / trend_down`（**没有 distribution**）
    - 极端标签（`extreme_*`）随 extreme 模型一起移除

    旧断言写的是 `g == a == {...四个...}`，A 股降到 3 态后它必然红——
    而"两个资产词表必须相等"这个前提本身就不成立（K 不同）。
    """
    g, a = set(gold_label.VALUES), set(ash_label.VALUES)

    # 共同的三态：涨/跌/震荡，两边都有
    base = {"range", "trend_up", "trend_down"}
    assert base <= g and base <= a, f"共同三态缺失：gold={g} ashare={a}"

    # `distribution` 是黄金 K=4 特有的，A 股 K=3 不该有
    assert "distribution" in g, g
    assert "distribution" not in a, a

    assert "panic" not in a and "rebound" not in a
    assert "stress" not in g and "recovery" not in g
    # 极端标签由规则层给出过，已随 extreme 模型删除，不该回流到任何资产
    for lab in ("extreme_up", "extreme_down", "extreme_vol"):
        assert lab not in a and lab not in g


def test_regime_valid_uses_asset_own_enum():
    g, a = _shared_edge(gold_cfg.EDGES), _shared_edge(ash_cfg.EDGES)
    assert set(g.regime_valid) <= set(gold_label.VALUES)
    assert set(a.regime_valid) <= set(ash_label.VALUES)


def test_edges_are_independent_objects():
    g, a = _shared_edge(gold_cfg.EDGES), _shared_edge(ash_cfg.EDGES)
    assert g is not a
    for e1 in gold_cfg.EDGES:
        for e2 in ash_cfg.EDGES:
            if e1.key == e2.key:
                assert e1 is not e2, "同一 key 的边不能共享实例"
