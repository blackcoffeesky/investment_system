"""验收 2：同一条边的 latency / regime_valid 必须按资产独立配置。

`FED_RATE -> 标的` 这条边在两个资产上同时存在，但：
- 黄金：机制是"实际利率/机会成本"，latency 以月计；
- A 股：机制是"外资风险偏好 + 国内流动性"，latency 以日计。
两者不能共享同一个 Edge 实例，也不能共享同一份 regime_valid。
"""

from __future__ import annotations

from causual.causual_assets.gold import config as gold_cfg
from causual.causual_assets.gold import label as gold_label
from causual.causual_assets.ashare import config as ash_cfg
from causual.causual_assets.ashare import label as ash_label


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
    g, a = set(gold_label.VALUES), set(ash_label.VALUES)
    # v5.9 起常规 HMM 只产出四个标签，两支资产一致；差异体现在极端/遗留枚举上
    assert g == a == {"range", "trend_up", "trend_down", "distribution"}
    assert "panic" not in a and "rebound" not in a
    assert "stress" not in g and "recovery" not in g
    # 极端标签由规则层给出，不在 HMM 词表里
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
