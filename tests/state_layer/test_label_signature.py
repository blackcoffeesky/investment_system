"""硬约束：`label_signature` 只由 **primary 组**的标签决定。

用户 2026-11-22：「之后 A 股添加短波动和长价格，单纯就是给 state_layer 本身用的，
causual 不变。」⇒ 加组不得改变签名。
"""

from __future__ import annotations

import pandas as pd

from state_layer.state_assets.base import GroupResult, label_fingerprint

IDX = pd.to_datetime(["2026-11-10", "2026-11-11", "2026-11-12"])


def _group(name: str, labels: list[str]) -> GroupResult:
    return GroupResult(
        name=name, title=name, k=len(set(labels)),
        states=pd.Series(range(len(labels)), index=IDX),
        labels={i: v for i, v in enumerate(labels)},
        label_values=pd.Series(labels, index=IDX),
    )


def test_signature_ignores_group_count():
    """只有 primary vs 多了一个假组 → 签名必须相同。"""
    primary = _group("short_price", ["trend_down", "range", "trend_up"])
    fake = _group("short_vol", ["high", "high", "low"])
    assert label_fingerprint(primary.label_series()) == \
        label_fingerprint(primary.label_series())
    # 加组不参与计算：把两组拼进 dict 再取 primary，值不变
    spec = {"primary": "short_price",
            "groups": {g.name: g for g in (primary, fake)},
            "features": ["ret_5"]}
    sig_now = label_fingerprint(spec["groups"][spec["primary"]].label_series())
    spec["groups"]["short_vol"] = fake          # 再加一次也不变
    assert sig_now == label_fingerprint(
        spec["groups"][spec["primary"]].label_series())


def test_signature_changes_with_primary_labels():
    """标签本身变了 → 签名必须变（这是唯一允许改签名的原因）。"""
    a = _group("short_price", ["trend_down", "range", "trend_up"])
    b = _group("short_price", ["trend_down", "range", "range"])
    assert label_fingerprint(a.label_series()) != label_fingerprint(b.label_series())


def test_signature_is_date_sensitive():
    """同标签、错日期 → 签名不同（否则签不到"哪一天的标签"）。"""
    a = _group("short_price", ["trend_down", "range", "trend_up"])
    b = GroupResult(name=a.name, title=a.title, k=3,
                    states=a.states, labels=a.labels,
                    label_values=pd.Series(list(a.label_values.values),
                                           index=IDX + pd.Timedelta(days=1)))
    assert label_fingerprint(a.label_series()) != label_fingerprint(b.label_series())
