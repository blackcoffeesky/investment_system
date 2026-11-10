"""端到端测试：从空库到出卡，验证整条链路。"""
import shutil
import sqlite3
import tempfile
from pathlib import Path

import pandas as pd


def _seed(db: Path) -> None:
    """塞入最小可用的数据：价格 + 两条信号层用的序列。"""
    from datahub.core.conn import connect
    from datahub.core.schema import DATA_DDL

    con = sqlite3.connect(str(db))
    con.executescript(DATA_DDL)
    dates = pd.bdate_range("2024-01-01", "2024-06-28")

    def put(table, series, kind="single", market="CN", freq="D", lag=0):
        con.execute(
            "INSERT INTO metadata(name, kind, market, freq, lag_days, start_date, end_date)"
            " VALUES (?,?,?,?,?,?,?)",
            (table, kind, market, freq, lag, str(series.index[0].date()),
             str(series.index[-1].date())))
        for d, v in series.items():
            con.execute(f"INSERT INTO {table}(date, value) VALUES (?,?)",
                        (str(d.date()), float(v)))

    import numpy as np

    rng = np.random.default_rng(7)
    close = pd.Series(1000 * np.exp(np.cumsum(rng.normal(0, 0.01, len(dates)))), index=dates)
    con.execute("CREATE TABLE csi_all(date TEXT PRIMARY KEY, value REAL)")
    put("csi_all", close)
    con.execute("CREATE TABLE vix(date TEXT PRIMARY KEY, value REAL)")
    put("vix", pd.Series(18 + rng.normal(0, 2, len(dates)), index=dates), market="US")
    con.commit()
    con.close()


def test_e2e_cards_from_empty_db():
    """空库 → 建表 → 出卡：至少不崩、产物落盘。"""
    tmp = Path(tempfile.mkdtemp())
    try:
        db = tmp / "e2e.sqlite"
        _seed(db)

        from datahub.core.conn import connect

        con = connect(db)
        n = con.execute("SELECT COUNT(*) FROM csi_all").fetchone()[0]
        con.close()
        assert n > 100, n

        # 数据在库、metadata 有登记
        con = connect(db)
        rows = con.execute("SELECT name FROM metadata").fetchall()
        con.close()
        assert {"csi_all", "vix"} <= {r[0] for r in rows}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cards_api_on_temp_dir():
    """list_cards / get_card 在临时目录下的行为。"""
    import causual.port as cp

    tmp = Path(tempfile.mkdtemp())
    old = cp.OUTPUT
    cp.OUTPUT = tmp
    try:
        d = tmp / "ashare" / "cards" / "2026" / "11"
        d.mkdir(parents=True)
        (d / "card_2026-11-04.md").write_text("# 测试卡\n", encoding="utf-8")

        items = cp.get_port().list_cards("ashare")
        assert len(items) == 1 and items[0].date == "2026-11-04"
        assert cp.get_port().get_card("ashare", "2026-11-04").startswith("# 测试卡")
        assert cp.get_port().to_html("# 标题").startswith("<h1"), cp.get_port().to_html("# 标题")
    finally:
        cp.OUTPUT = old
        shutil.rmtree(tmp, ignore_errors=True)


def test_sections_render_daily_vs_macro():
    """narrative 返回 section 列表，调用方按需挑选。"""
    from causual.causual_core.narrative import Section, render

    secs = [
        Section("header", ""),
        Section("state", "市场状态"),
        Section("structure", "结构层"),
        Section("supply", "供需层"),
        Section("signal", "净指向分解"),
    ]
    secs[0].lines = ["# X 因果状态卡 · 2026-11-04", "", "> α(当期归因) = +0.1"]
    for sec in secs[1:]:
        sec.lines = [f"{sec.title}正文", ""]

    daily = render(secs, keep=("state", "signal"))
    macro = render(secs, keep=("structure", "supply"),
                   title="# X 因果状态卡 · 宏观层 · 2026-09")

    assert "市场状态" in daily and "净指向分解" in daily
    assert "结构层" not in daily and "供需层" not in daily
    assert "α(" in daily

    assert "结构层" in macro and "供需层" in macro
    assert "净指向分解" not in macro
    assert macro.splitlines()[0].endswith("2026-09")
    # 编号连续
    assert "## 1. 结构层" in macro and "## 2. 供需层" in macro


def test_sections_render_all_when_keep_none():
    from causual.causual_core.narrative import Section, render

    secs = [Section("header", ""), Section("state", "状态"), Section("signal", "信号")]
    secs[0].lines = ["# T"]
    secs[1].lines = ["a"]
    secs[2].lines = ["b"]
    full = render(secs)
    assert "## 1. 状态" in full and "## 2. 信号" in full


def test_macro_card_is_monthly_and_idempotent():
    """月频卡按月份命名；同月重复检查不重复生成。"""
    from causual.port import card_path

    assert card_path("ashare", "2026-09", "macro").name == "macro_2026-09.md"
    assert card_path("ashare", "2026-09", "macro_detail").name == "macro_detail_2026-09.md"
    assert card_path("ashare", "2026-11-04", "card").name == "card_2026-11-04.md"


def test_write_upsert_single():
    """单值写入：新增 / 覆盖 / 元数据同步。"""
    import shutil
    import tempfile

    import datahub.fin.write as W
    from datahub.core.conn import connect
    from datahub.core.schema import DATA_DDL

    tmp = Path(tempfile.mkdtemp()) / "w.sqlite"
    import sqlite3
    con = sqlite3.connect(str(tmp))
    con.executescript(DATA_DDL)
    con.commit()
    con.close()

    try:
        r1 = W.upsert_single("t_probe", "2026-11-01", 1.0, db_path=tmp)
        assert r1["before"] is None and r1["after"] == 1.0
        r2 = W.upsert_single("t_probe", "2026-11-01", 2.0, db_path=tmp)
        assert r2["before"] == 1.0 and r2["after"] == 2.0
        # 元数据被补上并同步
        con = connect(tmp)
        row = con.execute("SELECT start_date, end_date FROM metadata WHERE name='t_probe'").fetchone()
        con.close()
        assert row[0] == "2026-11-01" and row[1] == "2026-11-01"
    finally:
        shutil.rmtree(tmp.parent, ignore_errors=True)


def test_data_page_renders():
    """数据层页面能渲染（含上传表单与表清单）。"""
    from fastapi.testclient import TestClient

    from service.app import app

    r = TestClient(app).get("/data")
    assert r.status_code == 200
    assert "上传文件" in r.text and "录入单个数值" in r.text


def test_fit_page_renders():
    """拟合页能渲染，并显示产物时间。"""
    from fastapi.testclient import TestClient

    from service.app import app

    r = TestClient(app).get("/fit")
    assert r.status_code == 200
    assert "状态模型拟合" in r.text and "重估边" in r.text


def test_modules_have_web_package():
    """每个模块自带 web 子包，service 不再持有模块页面。"""
    import importlib

    for mod in ("datahub.web", "causual.web", "datahub.port", "causual.port"):
        importlib.import_module(mod)


def test_diagnose_page_renders():
    """诊断页：画像表 + SVG 时间轴 + 观测矩阵都要出来。"""
    from fastapi.testclient import TestClient

    from service.app import app

    r = TestClient(app).get("/diagnose")
    assert r.status_code == 200
    assert "<svg" in r.text, "时间轴没画出来"
    assert "各状态画像" in r.text and "观测矩阵" in r.text


def test_diagnose_excludes_causual_inputs():
    """诊断层不许读 causual 的信号输入（两套系统输入不相交）。"""
    from diagnose import observations

    used = {t for o in observations.OBSERVATIONS for t in o.sources}
    assert not (used & set(observations.EXCLUDED_TABLES)), used
