"""资产层抽象的**纯数据**部分：`GroupResult` → 长表 / 标签映射。

`store.save_estimate(groups=…)` 认的就是这里产出的长表，所以先把形状钉死。
"""

from __future__ import annotations

import pandas as pd

from state_layer.state_assets.base import GROUP_COLUMNS, GroupResult, to_long_df

IDX = pd.to_datetime(["2026-11-10", "2026-11-11"])


def _grp(name: str = "short_price") -> GroupResult:
    return GroupResult(
        name=name,
        title="短价格",
        k=2,
        states=pd.Series([0, 1], index=IDX),
        labels={0: "trend_down", 1: "trend_up"},
        proba=pd.DataFrame({"p0": [0.9, 0.2], "p1": [0.1, 0.8]}, index=IDX),
        features=("ret_5",),
        fit_id="abc123",
    )


def test_label_series_maps_ids_to_stable_labels():
    """对外只暴露字符串：编号重训会换位。"""
    g = _grp()
    assert list(g.label_series()) == ["trend_down", "trend_up"]
    assert g.label_series().name == "label"
    assert list(g.label_series().index) == list(IDX)


def test_rows_match_store_contract():
    """行形状必须等于 `GROUP_COLUMNS`，p_max 取后验最大。"""
    rows = _grp().rows()
    assert list(rows.columns) == list(GROUP_COLUMNS)
    assert list(rows["group"]) == ["short_price", "short_price"]
    assert list(rows["state"]) == [0, 1]
    assert [round(float(x), 6) for x in rows["p_max"]] == [0.9, 0.8]


def test_rows_without_proba_still_has_columns():
    g = _grp()
    g.proba = None
    rows = g.rows()
    assert list(rows.columns) == list(GROUP_COLUMNS)
    assert rows["p_max"].isna().all()


def test_to_long_df_concatenates_groups():
    long = to_long_df({"short_price": _grp(), "short_vol": _grp("short_vol")})
    assert list(long.columns) == list(GROUP_COLUMNS)
    assert len(long) == 4
    assert set(long["group"]) == {"short_price", "short_vol"}


def test_to_long_df_empty_keeps_schema():
    long = to_long_df({})
    assert long.empty
    assert list(long.columns) == list(GROUP_COLUMNS)
