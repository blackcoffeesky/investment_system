"""端到端测试：从空库到出卡，验证整条链路。"""

import shutil
import sqlite3
import tempfile
from pathlib import Path

import pandas as pd

#: 运行超过 10 秒（`run_tests.py --fast` 会跳过）
SLOW = True


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
    from causual.web.render import Section, render

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
    from causual.web.render import Section, render

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
    # `hmm_last.pkl` 已降级成遗留产物（重拟合后是旧货、也没人再写它），
    # 那个"在/缺失"指示灯会永远说"在"——已删，别让它回流。
    assert "HMM 模型文件" not in r.text
    assert "hmm_last" not in r.text


def test_modules_have_web_package():
    """每个模块自带 web 子包，service 不再持有模块页面。"""
    import importlib

    for mod in ("datahub.web", "causual.web", "datahub.port", "causual.port"):
        importlib.import_module(mod)


def test_state_page_renders():
    """诊断页：分组画像 + 多张 SVG 状态图。

    2026-11-23：**观测矩阵整体移除**（用户要求），所以这里反过来了——
    断言它**不在**页面上。删功能要能证明它真从页面消失了，不是"我以为删了"。
    """
    from fastapi.testclient import TestClient

    from service.app import app

    r = TestClient(app).get("/state")
    assert r.status_code == 200
    assert "<svg" in r.text, "时间轴没画出来"
    assert "各状态画像" in r.text, "分组画像没出来"
    assert "观测矩阵" not in r.text, "观测矩阵已移除，不该再出现"
    # A 股三组 + 九宫格 = 4 张图
    assert r.text.count("<svg") >= 4, f"图数不足：{r.text.count('<svg')}"


def test_regenerate_card_returns_303():
    """「重新生成卡片」按钮真的能跑通（2026-11-18 用户点了 500 的那个）。

    失败时的报错是 `RuntimeError: ashare 缺少状态模型的 spec`——路由只传了
    `panel` / `freshness`，**漏了 `state`**（`build_panel` 要 `state["spec"]`）。
    这类漏参数不会在 import 期暴露，只能靠"真发一次 POST"。

    只断言不 500 + 重定向，不比对内容：内容是数据/模型的函数，比对会随数据漂。
    （逐字对账另做：同一份数据下重生成的卡与原卡一致，只有 `generated_at` 不同。）
    """
    from fastapi.testclient import TestClient

    from causual.port import get_port
    from service.app import app

    latest = get_port().latest("ashare")
    assert latest is not None, "没有已生成的卡可重生成"
    date = latest.date

    r = TestClient(app, follow_redirects=False).post(
        f"/card/ashare/{date}/regenerate")
    assert r.status_code == 303, f"重生成失败：{r.status_code} {r.text[:400]}"
    assert r.headers.get("location") == f"/card/ashare/{date}"
    assert get_port().exists("ashare", date), "重生成后卡不见了"


def test_state_excludes_causual_inputs():
    """诊断层不许读 causual 的信号输入（两套系统输入不相交）。

    2026-11-23：观测矩阵（`state_layer/analysis/observations.py`）已整体移除，
    这条测试原先靠遍历 `OBSERVATIONS` 证明"不读 causual 表"。
    断言对象换成**状态层实际读的表**（`port.required_tables`）——
    原来查的是"意图清单"，现在查"真读了什么"，更直接。
    """
    from state_layer.port import get_port

    causual_only = {"vix", "usdcny", "usd_index", "fed_rate", "real_yield_10y",
                    "shibor3m", "margin_balance", "etf_shares", "insider_net"}
    used = set(get_port().required_tables("ashare"))
    assert not (used & causual_only), f"状态层读了 causual 的表：{used & causual_only}"


def test_state_contract_fields():
    """状态层输出契约：下游只依赖这几个字段。"""
    from state_layer.port import get_port

    cur = get_port().current_state("ashare")
    for k in ("date", "asset", "state_id", "label", "state_probabilities",
              "confidence", "model", "as_of_date"):
        assert k in cur, f"契约缺字段 {k}"
    # version 形如 `<链>-k<K>-<fit_id>`：单模型是 `hmm-k3-…`，链式是
    # `extreme+hmm-k3-…`（按 MODEL_CHAIN 生成）。别写死前缀——换链就误报。
    # 版本串按**结构**断言，不写死前缀——写死 `hmm-k` 这次就误报了（换模型必炸）。
    ver, k = cur["model"]["version"], cur["model"]["k"]
    assert ver.startswith("estimator-v2:"), ver
    assert f"@{cur['group']}" in ver, ver
    assert f"-k{k}-" in ver, ver
    assert "hmm" in str(cur["model"].get("kind") or ""), cur["model"]



def test_state_extend_fills_gap():
    """外推：给一段带空标签的序列，断档期应被填上，且不动已有标签。"""
    import numpy as np
    import pandas as pd

    from state_layer import state_assets
    from state_layer.port import get_port

    port = get_port()
    labels = port.labels("ashare")
    if labels.empty:
        return
    spaced = labels.reindex(
        pd.bdate_range(labels.index[0], labels.index[-1] + pd.Timedelta(days=20))
    ).fillna("").astype(str)

    # 2026-11-22 接口变更：`extend` 的第二参数从 **features 矩阵** 变成 **panel**。
    # 原因：特征现在属于分组（A 股三组各一套），提前算特征等于把特征定义泄漏到每个调用点。
    # 所以这里喂真实面板，不再伪造随机特征矩阵。
    from shared.adapter import Panel, get_data

    panel = get_data(Panel.STATE_ASHARE, start=str(spaced.index[0].date()))
    out = port.extend("ashare", panel, spaced)
    assert out.index.max() > labels.index.max(), "没有外推"
    # 只能要求"**面板里有的交易日**都填上"：`spaced` 是按工作日生成的，
    # 节假日（A 股休市）没有面板行、也不该有标签。旧的 `(out != "").all()`
    # 之所以能过，是因为旧测试伪造了一个覆盖所有工作日的特征矩阵——那不是真实形状。
    filled_days = out[out.index > labels.index.max()]
    assert (filled_days.loc[filled_days.index.isin(panel.index)] != "").all(), \
        "外推结果在面板交易日上还有空标签"
    keep = out.reindex(labels.index)
    assert (keep == labels).all(), "已有标签被改动"
