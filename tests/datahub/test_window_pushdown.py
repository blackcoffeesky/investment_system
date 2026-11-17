"""窗口 / codes 过滤必须**下推到 SQL**，且不改变数值。

2026-11-16 之前 `series_map(names)` 一个参数都不传：`stock_daily` 1040 万行被整张
读进内存再丢掉大半。现在 `start` / `end` / `codes` 一路落到 SQL（`WHERE date >= ?
AND date <= ?` / `WHERE code IN (...)`）。

这组测试钉两件事：

1. **窗口等价**：`series_map([t], start, end)` 的结果 == `series_map([t])` 再切窗口。
   **三种 kind 各验一次**（multi / single / long）——防的是"某个 kind 忘了传 start/end"
   （那种 bug 不会报错，只会让过滤悄悄失效、慢十倍）。
2. **codes 真的到了 SQL**：`stock_daily` 只取一只时，行数应该是"半年交易日量级"，
   key 只有那一个；如果还是 5578 个 key，就说明过滤没下推。
"""

from __future__ import annotations

import contextlib
import io

import pandas as pd

from datahub.port import get_port

START, END = "2025-01-01", "2025-06-30"

#: 三种 kind 各一个代表：multi / single / long
CASES = ("csi_all", "csi300", "etf_shares")


def _quiet(fn, *a, **kw):
    """吞掉 series_map 的 stderr 提示（static 跳过、codes 忽略），测试只看结果。"""
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        out = fn(*a, **kw)
    return out, buf.getvalue()


def test_window_equals_full_slice_by_kind():
    port = get_port()
    for name in CASES:
        full, _ = _quiet(port.series_map, [name])
        win, _ = _quiet(port.series_map, [name], start=START, end=END)
        assert full, f"{name} 全量读出来是空的，测试前提不成立"
        assert set(win) <= set(full), f"{name} 窗口多出了全量没有的 key"
        for key, got in win.items():
            expect = full[key].loc[START:END]
            # 用 assert_series_equal 而不是 `(a-b).max() == 0`：
            # 有些列在窗口内**整列 NaN**（如 csi_all.amount 早期），相减会得到 NaN，
            # 那种写法会把"两边都空"误判成"不一致"。
            pd.testing.assert_series_equal(expect, got, check_exact=True,
                                           check_names=False, check_freq=False)


def test_long_keys_shrink_with_window():
    """long 表的 key 随窗口收缩是**预期行为**（窗口内没数据的代码不出现），
    但要保证"只少不多"——不能凭空多出全量里没有的代码。"""
    port = get_port()
    full, _ = _quiet(port.series_map, ["etf_shares"])
    win, _ = _quiet(port.series_map, ["etf_shares"], start=START, end=END)
    assert set(win) <= set(full)
    assert len(win) <= len(full)


def test_static_is_skipped_with_a_warning():
    port = get_port()
    out, err = _quiet(port.series_map, ["company"])
    assert out == {}, "static 表不该产出时间序列"
    assert "static" in err, "跳过 static 时必须告警（不然调用方以为读到了）"


def test_codes_reach_sql():
    """`stock_daily` 只取一只：key 只有一个、行数是"半年量级"。
    如果过滤没下推，这里会是 5578 个 key（测试会直接红）。"""
    port = get_port()
    out, _ = _quiet(port.series_map, ["stock_daily"], codes=["sh600519"],
                    start="2026-01-01", end="2026-06-30")
    assert list(out) == ["stock_daily:sh600519"], f"key 不对：{list(out)[:5]}"
    s = out["stock_daily:sh600519"]
    assert 90 <= len(s) <= 150, f"半年的行数应该在 90~150 之间，实际 {len(s)}（过滤没下推？）"
    assert s.index[0] >= pd.Timestamp("2026-01-01")


def test_codes_on_non_long_warns_and_returns_full():
    """codes 打到 single/multi：**告警 + 返回全量**，不能崩、也不能返回空。"""
    port = get_port()
    full, _ = _quiet(port.series_map, ["csi_all"])
    out, err = _quiet(port.series_map, ["csi_all"], codes=["sh600519"])
    assert len(out) == len(full) > 0, "打到非 long 表时应返回全量"
    assert "codes" in err and "忽略" in err, f"必须告警，实际 stderr：{err!r}"
