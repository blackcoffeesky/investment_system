"""耦合层契约：`shared.adapter.get_data` 是消费方唯一取数入口。

这组测试盯的是**接口**，不是某天的数据：
1. 四个 `Panel` 都返回非空 DataFrame，**列集合恰好等于** `COLUMNS[p]`（少一列/多一列都算违约）
2. index 是 `DatetimeIndex`、单调升、无重复
3. 新鲜度：最后一行不早于「今天 − 5 天」（管线停了要红——这是它的用途）
4. 无前视：最后一行不晚于今天
5. **一致性**：`STATE_ASHARE` 与 `CAUSUAL_ASHARE` 的同名列在同一日期上数值相等
   ——这条保证"底层 builder 只有一份"，两份实现一旦漂移就会红
6. `import shared` **不许**把 datahub / sqlite 拖进来（纯工具层必须无副作用）

第 3 条用的是自然日而非交易日：它本来就是"管线有没有在跑"的粗检，
不是行情日历。如果某天故意不更新数据，这条会红——那是正确行为。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pandas as pd

from shared.adapter import (COLUMNS, Panel, column_of, get_data, tables_of)

ROOT = Path(__file__).resolve().parents[1]

#: 面板最后一行允许落后"今天"的自然日数
FRESH_DAYS = 5

_CACHE: dict[Panel, pd.DataFrame] = {}


def _panel(p: Panel) -> pd.DataFrame:
    """读一次缓存一份（四个测试共用，避免重复读库）。"""
    if p not in _CACHE:
        _CACHE[p] = get_data(p)
    return _CACHE[p]


def _assert_contract(p: Panel) -> None:
    df = _panel(p)
    assert isinstance(df, pd.DataFrame), f"{p.name} 不是 DataFrame"
    assert len(df) > 0, f"{p.name} 是空的"
    want, got = set(COLUMNS[p]), set(df.columns)
    assert got == want, (
        f"{p.name} 列集合与契约不符：多 {sorted(got - want)}，少 {sorted(want - got)}")
    # 列顺序也按声明（下游 subset 依赖它，顺序漂了不好查）
    assert list(df.columns) == list(COLUMNS[p]), f"{p.name} 列顺序与声明不一致"


def test_state_ashare_contract():
    _assert_contract(Panel.STATE_ASHARE)


def test_state_gold_contract():
    _assert_contract(Panel.STATE_GOLD)


def test_causual_ashare_contract():
    _assert_contract(Panel.CAUSUAL_ASHARE)


def test_causual_gold_contract():
    _assert_contract(Panel.CAUSUAL_GOLD)


def test_index_is_datetime_ascending_unique():
    for p in Panel:
        df = _panel(p)
        assert isinstance(df.index, pd.DatetimeIndex), f"{p.name} 的 index 不是 DatetimeIndex"
        assert df.index.is_monotonic_increasing, f"{p.name} 的 index 不是升序"
        assert not df.index.has_duplicates, f"{p.name} 的 index 有重复日期"
        assert df.index.tz is None, f"{p.name} 的 index 带时区（应为本地日期）"


def test_last_row_is_fresh_and_not_future():
    today = pd.Timestamp.today().normalize()
    for p in Panel:
        last = _panel(p).index[-1]
        assert last <= today, f"{p.name} 最后一行 {last.date()} 晚于今天（前视）"
        lag = int((today - last).days)
        assert lag <= FRESH_DAYS, (
            f"{p.name} 最后一行停在 {last.date()}，落后今天 {lag} 天"
            f"（阈值 {FRESH_DAYS}）——数据管线是不是停了？")


def test_state_and_causual_ashare_agree_on_shared_columns():
    """同一份 builder 出的列必须逐值相同。

    两个 Panel 的 ashare_base 是同一段代码；一旦有人给某个 Panel 单独加特判
    （"顺手"补一列、改个对齐），这里立刻红。
    """
    state = _panel(Panel.STATE_ASHARE)
    causal = _panel(Panel.CAUSUAL_ASHARE)
    shared_cols = [c for c in state.columns if c in causal.columns]
    assert shared_cols, "两者没有任何同名列，契约结构变了？"

    idx = state.index.intersection(causal.index)
    assert len(idx) > 0, "两个面板没有共同日期"
    left = state.loc[idx, shared_cols]
    right = causal.loc[idx, shared_cols]
    pd.testing.assert_frame_equal(
        left, right, check_exact=True, check_names=True, check_like=False,
        obj=f"{'STATE_ASHARE vs CAUSUAL_ASHARE'} 同名列不一致")


def test_column_of_and_bare_code_conventions():
    """两个约定 helper：`panel.py` 靠它们把旧映射接到语义列上。"""
    from datahub.fin.derive import bare_code

    assert column_of(("csi_all", "close")) == "close"
    assert column_of("csi300") == "csi300"
    for raw, want in (("sh510300", "510300"), ("sz159919", "159919"),
                      ("c510300", "510300"), ("510300", "510300"),
                      ("510300.SH", "510300")):
        assert bare_code(raw) == want, f"{raw} 归一成 {bare_code(raw)}，应为 {want}"


def test_shared_init_does_not_import_datahub():
    """`import shared` 必须保持"纯"——不能把 datahub / sqlite 拖进来。

    否则任何只想用 `shared.utils.calendar` 的模块都会顺带打开数据库，
    纯工具层的约束就名存实亡了。
    """
    code = (
        "import sys, shared\n"
        "bad = [m for m in sys.modules if m == 'datahub' or m.startswith('datahub.')]\n"
        "print(bad)\n"
        "raise SystemExit(1 if bad else 0)\n"
    )
    r = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                       capture_output=True, text=True)
    assert r.returncode == 0, (
        f"`import shared` 把 datahub 拖进来了：{r.stdout.strip()} {r.stderr.strip()}")


def test_tables_of_matches_datahub_metadata():
    """`tables_of` 声明的表必须都真的存在（防改名后只改了 spec、忘了表名）。"""
    from datahub.port import get_port

    known = set(get_port().metadata()["name"])
    for p in Panel:
        tables = tables_of(p)
        assert tables, f"{p.name} 的 tables_of 是空的"
        missing = set(tables) - known
        assert not missing, f"{p.name} 声明了不存在的表：{sorted(missing)}"


def test_raw_columns_are_not_ffilled():
    """`raw` 列**不允许被 ffill 过**——这是本轮的核心行为改变。

    如果哪天有人把策略从 raw 改回 daily（先 ffill 再算变化率），
    `ETF_DISPERSION` 会静默换一条特征（实测近 250 日均值 -72%、66% 的日期变 0），
    标签跟着变、边快照全部失效，而且**不报错**。所以在这里钉死。
    """
    for p in Panel:
        df = _panel(p)
        raw_cols = [c for c, how in COLUMNS[p].items() if how == "raw"]
        if not raw_cols:
            continue                      # STATE_GOLD 全是日频，没有 raw 列
        for c in raw_cols:
            assert df[c].isna().any(), (
                f"{p.name}.{c} 是 raw 列却没有任何 NaN——被 ffill 了？")
            assert not df[c].equals(df[c].ffill()), (
                f"{p.name}.{c} 与 ffill 后完全相同——raw 策略失效")
