"""失败分类 `transient` / `structural` + 临时故障重试。

2026-11-25 的噪声告警：FRED 一次抖动 → `vix` / `usdcny` / `usd_index` 三张表失败 →
状态机把整条 `fetch` 标成 **error**（其中 `shibor3m` 恰在保鲜期内被判过期），
用户每天看到一条 error，实际只是网络问题。

方案：**按失败原因分类**（不靠字符串），`transient` 先重试 2 次；
上层据此把"全 transient 的失败"降级成 `warn`，有 `structural` 仍报 `error`。
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pandas as pd
import requests

from datahub.fin import update as U
from datahub.fin.retry import call_with_retry, classify_error, is_transient
from datahub.port import UpdateReport


class RemoteDataError(Exception):
    """模拟 `pandas_datareader` 的 `RemoteDataError`（FRED 取不到 URL 时抛的）。"""


def test_transient_errors_are_classified_transient():
    for exc in (requests.exceptions.ConnectionError("x"),
                requests.exceptions.ReadTimeout("x"),
                requests.exceptions.ConnectTimeout("x"),
                TimeoutError("x"),
                ConnectionResetError("x"),
                RemoteDataError("Unable to read URL")):
        assert classify_error(exc) == "transient", (type(exc).__name__, classify_error(exc))
        assert is_transient(exc) is True


def test_structural_errors_are_classified_structural():
    """**拿不准的一律 structural**（宁可吵，不可静默）。"""
    for exc in (KeyError("close"), ValueError("bad parse"), TypeError("x"),
                RuntimeError("列名对不上"), AttributeError("no attr")):
        assert classify_error(exc) == "structural", (type(exc).__name__, classify_error(exc))


def test_report_all_transient_true_only_when_all_failures_are_transient():
    only_net = UpdateReport(failed=2, details=[
        {"table": "vix", "status": "失败", "kind": "transient"},
        {"table": "usdcny", "status": "失败", "kind": "transient"}])
    assert only_net.all_transient is True

    mixed = UpdateReport(failed=2, details=[
        {"table": "vix", "status": "失败", "kind": "transient"},
        {"table": "csi_all", "status": "失败", "kind": "structural"}])
    assert mixed.all_transient is False
    # 没失败 → False（没失败不需要降级）
    assert UpdateReport(ok=3, details=[{"table": "vix", "status": "ok"}]).all_transient is False


def test_call_with_retry_succeeds_after_transient_failure():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise requests.exceptions.ConnectionError("boom")
        return "ok"

    assert call_with_retry(flaky, retries=2, delays=(0, 0)) == "ok"
    assert calls["n"] == 3, f"应该重试到第 3 次，实际 {calls['n']}"


def test_call_with_retry_does_not_retry_structural():
    calls = {"n": 0}

    def broken():
        calls["n"] += 1
        raise KeyError("close")

    try:
        call_with_retry(broken, retries=2, delays=(0, 0))
    except KeyError:
        pass
    else:
        raise AssertionError("structural 必须直接抛，不重试")
    assert calls["n"] == 1, f"structural 不该重试，实际调用 {calls['n']} 次"


def test_run_retries_transient_then_succeeds(monkeypatch_none=None):
    """**集成**：源函数第一次抛 transient、第二次成功 → `run()` 里不计失败。"""
    db = Path(tempfile.gettempdir()) / "dh_retry_run.sqlite"
    if db.exists():
        db.unlink()
    con = sqlite3.connect(str(db))
    con.executescript(U.DATA_DDL)
    con.commit()
    con.close()

    state = {"n": 0}

    def flaky_source(**_) -> pd.Series:
        state["n"] += 1
        if state["n"] == 1:
            raise requests.exceptions.ConnectionError("首次抖动")
        return pd.Series([1.0, 2.0], index=pd.DatetimeIndex(["2026-11-20", "2026-11-21"]))

    orig_sources = U.SOURCES
    orig_db = U.DB_PATH
    try:
        U.SOURCES = [U.Source("retry_probe", flaky_source, {}, note="测试用")]
        U.DB_PATH = db                               # type: ignore[assignment]
        df = U.run(start="2026-11-20", end="2026-11-21", force=True)
    finally:
        U.SOURCES = orig_sources
        U.DB_PATH = orig_db                          # type: ignore[assignment]

    assert state["n"] == 2, f"应该重试一次，实际调用 {state['n']} 次"
    assert len(df) == 1 and df.iloc[0]["status"] == "ok", df.to_string()
    db.unlink()
