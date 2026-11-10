"""状态机的场景测试。

不依赖 pytest：函数名以 test_ 开头即可被 tests/run_tests.py 收集。
每个用例都用**临时数据库**，不碰真实的运维状态。
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pandas as pd

from datahub.ops import OpsStore
from service.ops.context import ServiceBag
from service.ops.manager import Manager
from service.ops.messages import MessageLevel, StateStatus
from service.ops.states import STATES


# ---------------------------------------------------------------- 替身
class FakeDS:
    """假的数据端口：只回答状态机关心的几个问题。"""

    def __init__(self, overdue_days: int = 1):
        self.overdue_days = overdue_days

    def run_update(self):
        class R:
            success = True
            failed = 0
            details = []

            def summary(self):
                return "假更新"
        return R()

    def metadata(self):
        return pd.DataFrame([
            {"name": "cb_holdings", "freq": "Q", "end_date": "2026-06-30"},
            {"name": "insider_net", "freq": "M", "end_date": "2026-09-30"},
        ])

    def covered_tables(self):
        return {"cb_holdings", "insider_net"}

    def read(self, name, **_):
        import numpy as np

        idx = pd.bdate_range("2026-10-01", "2026-11-04")
        return pd.Series(np.linspace(100, 110, len(idx)), index=idx, name=name)

    def check(self):
        return pd.DataFrame()


class FakeCards:
    def __init__(self, fail: set[str] | None = None):
        self.fail = fail or set()

    def required_tables(self, asset):
        return ("csi_all",)

    def last_data_date(self, asset, series):
        return "2026-11-02"

    def exists(self, asset, date):
        return False

    def generate(self, asset, series, meta=None, date=None):
        if asset in self.fail:
            raise RuntimeError("假失败")
        return Path(f"/tmp/{asset}_{date}.md")

    def generate_macro(self, asset, month, series, meta=None):
        if asset in self.fail:
            raise RuntimeError("假失败")
        return Path(f"/tmp/{asset}_{month}.md")

    def macro_month(self, meta=None):
        return "2026-11"

    def exists(self, asset, date, kind="card"):
        return False


def _tmp():
    d = tempfile.mkdtemp()
    return Path(d) / "ops.sqlite"


def _mgr(path, cards=None, ds=None):
    bag = ServiceBag({"datahub": ds or FakeDS(), "causual": cards or FakeCards()})
    return Manager(STATES, bag, path)


def _overdue(m, days=1):
    import service.ops.states.data as d
    d.EXPECTED_DAYS.update({"cb_holdings": days, "insider_net": days})


# ---------------------------------------------------------------- 用例
def test_crash_recovery_goes_blocked():
    """running → blocked + UNEXPECTED_INTERRUPT，不自动重跑。"""
    p = _tmp()
    m = _mgr(p)
    m.store.set_status("fetch", StateStatus.RUNNING.value, touch=True)

    m2 = _mgr(p)
    m2.recover()          # 恢复现在只发生在启动/run 开始时
    st = m2.status()["fetch"]
    assert st["state"] == "blocked", st
    assert st["blocking_msg_id"], "blocked 必须能定位到消息"

    msgs = m2.store.messages(unacked_only=True)
    assert any(x["level"] == "error" for x in msgs)


def test_error_cannot_be_skipped():
    p = _tmp()
    m = _mgr(p)
    m.store.save_result("fetch", StateStatus.BLOCKED.value,
                        messages=[{"id": "m-e1", "level": "error", "text": "坏了",
                                   "state": "fetch", "created_at": "2026-01-01"}],
                        blocking_msg_id="m-e1")
    r = m.ack("m-e1", "skip")
    assert not r["ok"] and r["allowed"] == ["retry"]


def test_error_retry_resets_state():
    p = _tmp()
    m = _mgr(p)
    m.store.save_result("fetch", StateStatus.BLOCKED.value,
                        messages=[{"id": "m-e2", "level": "error", "text": "坏了",
                                   "state": "fetch", "created_at": "2026-01-01"}],
                        blocking_msg_id="m-e2")
    r = m.ack("m-e2", "retry")
    assert r["ok"] and r["result"] == "rerun"
    assert m.status()["fetch"]["state"] == "pending"


def test_info_only_allows_ack():
    p = _tmp()
    m = _mgr(p)
    m.store.save_result("validate", StateStatus.DONE.value,
                        messages=[{"id": "m-i1", "level": "info", "text": "通知",
                                   "state": "validate", "created_at": "2026-01-01"}])
    assert not m.ack("m-i1", "retry")["ok"]
    assert not m.ack("m-i1", "skip")["ok"]
    r = m.ack("m-i1", "ack")
    assert r["ok"] and r["result"] == "ack_only"
    assert m.status()["validate"]["state"] == "done", "INFO 的确认不应改状态"


def test_skip_once_keeps_checking():
    """skip(once) 记 SNOOZED：下次仍会重新检查。"""
    p = _tmp()
    m = _mgr(p)
    m.store.save_result("validate", StateStatus.WAITING_ACK.value,
                        messages=[{"id": "m-w1", "level": "warning", "text": "逾期",
                                   "state": "validate", "key": "cb_holdings",
                                   "period": "2026-06-30", "created_at": "2026-01-01"}],
                        blocking_msg_id="m-w1")
    r = m.ack("m-w1", "skip", remind_at="once")
    assert r["result"] == "skip"
    assert m.status()["validate"]["state"] == "snoozed"
    assert not m.store.snoozes("validate"), "once 不应留下静音记录"


def test_snooze_is_per_message():
    """静音一张表，不该把整个状态也静音。"""
    p = _tmp()
    m = _mgr(p)
    _overdue(m)
    m.store.set_status("fetch", StateStatus.DONE.value)
    m._refresh()

    r = m.run()
    assert r["validate"]["status"] == "waiting_ack"
    warns = [x for x in m.store.messages(unacked_only=True) if x["level"] == "warning"]
    assert len(warns) == 2, [w["key"] for w in warns]

    # 静音其中一张
    m.ack(warns[0]["id"], "skip", remind_at="period_change")
    m2 = _mgr(p)
    m2.store.set_status("fetch", StateStatus.DONE.value)
    m2._refresh()
    r2 = m2.run()
    assert r2["validate"]["status"] == "waiting_ack", "另一张仍应打断"

    # 把剩下的告警全部静音 → snoozed
    for x in [y for y in m2.store.messages(unacked_only=True)
              if y["level"] == "warning"]:
        m2.ack(x["id"], "skip", remind_at="period_change")
    m3 = _mgr(p)
    m3.store.set_status("fetch", StateStatus.DONE.value)
    m3._refresh()
    assert m3.run()["validate"]["status"] == "snoozed"


def test_snooze_expires_by_days():
    p = _tmp()
    m = _mgr(p)
    m.store.save_result("validate", StateStatus.WAITING_ACK.value,
                        messages=[{"id": "m-w2", "level": "warning", "text": "逾期",
                                   "state": "validate", "key": "cb_holdings",
                                   "period": "2026-06-30", "created_at": "2026-01-01"}],
                        blocking_msg_id="m-w2")
    m.ack("m-w2", "skip", remind_at="days:3")
    assert m.store.snoozes("validate")

    con = sqlite3.connect(str(p))
    con.execute("UPDATE ops_snooze SET remind_at='2000-01-01T00:00:00'")
    con.commit()
    con.close()

    m2 = _mgr(p)
    m2.store.set_status("fetch", StateStatus.DONE.value)
    m2._refresh()
    assert m2.run()["validate"]["status"] in ("waiting_ack", "blocked")


def test_repeated_run_does_not_bump_attempts():
    """日常重复运行不该让 attempts 一直涨。"""
    p = _tmp()
    m = _mgr(p)
    m.store.set_status("fetch", StateStatus.DONE.value)
    m._refresh()
    m.run()
    m._refresh()                      # status() 读的是内存快照，跑完要刷新
    first = int(m.status()["validate"]["attempts"])
    m.run()
    m._refresh()
    assert int(m.status()["validate"]["attempts"]) == first, m.status()["validate"]


def test_run_log_written():
    p = _tmp()
    m = _mgr(p)
    m.store.set_status("fetch", StateStatus.DONE.value)
    m._refresh()
    r = m.run()
    last = m.store.last_run()
    assert last and last["run_id"] == r["run_id"]
    assert last["finished_at"]


def test_generate_cards_warns_on_partial_failure():
    p = _tmp()
    m = _mgr(p, cards=FakeCards(fail={"gold"}))
    for s in ("fetch", "validate"):
        m.store.set_status(s, StateStatus.DONE.value)
    m._refresh()
    r = m.run(only=["generate_cards"])
    assert r["generate_cards"]["status"] == "waiting_ack", r
    msgs = [x for x in m.store.messages(unacked_only=True) if x["level"] == "warning"]
    assert any(x["key"] == "gold" for x in msgs)


def test_generate_cards_errors_when_all_fail():
    p = _tmp()
    m = _mgr(p, cards=FakeCards(fail={"ashare", "gold"}))
    for s in ("fetch", "validate"):
        m.store.set_status(s, StateStatus.DONE.value)
    m._refresh()
    r = m.run(only=["generate_cards"])
    assert r["generate_cards"]["status"] == "blocked", r


def test_ack_unknown_message():
    p = _tmp()
    m = _mgr(p)
    assert not m.ack("不存在", "retry")["ok"]


def test_reset_forces_pending():
    p = _tmp()
    m = _mgr(p)
    m.store.set_status("fetch", StateStatus.DONE.value)
    m._refresh()
    assert m.reset("fetch")["ok"]
    assert m.status()["fetch"]["state"] == "pending"
    assert not m.reset("不存在")["ok"]


def test_reset_all_refuses_when_blocked():
    """有 blocked/waiting_ack 时不允许整轮重跑（会绕过确认）。"""
    p = _tmp()
    m = _mgr(p)
    m.store.set_status("fetch", StateStatus.DONE.value)
    m.store.set_status("validate", StateStatus.WAITING_ACK.value)
    m._refresh()
    r = m.reset_all()
    assert not r["ok"] and "validate" in r["reason"]


def test_reset_all_resets_settled_states():
    """done/snoozed 全部回 pending，attempts 清零但保留 last_attempts。"""
    p = _tmp()
    m = _mgr(p)
    for name in [x.name for x in STATES]:
        m.store.set_status(name, StateStatus.DONE.value, attempts=3)
    m.store.set_status("validate", StateStatus.SNOOZED.value, attempts=5)
    m._refresh()

    assert m.all_settled(), m.status()
    r = m.reset_all()
    assert r["ok"], r
    for name in [x.name for x in STATES]:
        st = m.status()[name]
        assert st["state"] == "pending", (name, st)
        assert int(st["attempts"]) == 0, (name, st)
    assert int(m.status()["validate"]["last_attempts"]) == 5


def test_reset_all_keeps_snooze():
    """重跑不解除静音。"""
    p = _tmp()
    m = _mgr(p)
    m.store.snooze("validate", "cb_holdings", "period", period="2026-06-30")
    m.store.set_status("validate", StateStatus.SNOOZED.value)
    m._refresh()
    m.reset_all()
    assert m.store.snoozes("validate"), "重跑不应清掉静音"


def test_all_settled_and_has_unacked_flags():
    p = _tmp()
    m = _mgr(p)
    for name in [x.name for x in STATES]:
        m.store.set_status(name, StateStatus.DONE.value)
    m._refresh()
    assert m.all_settled() and not m.has_unacked()

    m.store.set_status("validate", StateStatus.WAITING_ACK.value)
    m._refresh()
    assert not m.all_settled() and m.has_unacked()


# ---------------------------------------------------------------- 新鲜度 / 页面
def test_freshness_uses_given_asof():
    """新鲜度必须按传入的参考日判断，不能用"今天"。

    11-03 的卡在 11-05 重建时，不应因为 11-04/11-05 的数据没到而报警。
    """
    import pandas as pd

    from causual.causual_core import freshness

    meta = pd.DataFrame([
        {"name": "csi_all", "freq": "D", "lag_days": 0, "end_date": "2026-11-03"},
        {"name": "vix", "freq": "D", "lag_days": 0, "end_date": "2026-11-03"},
    ])
    # 用 11-03 当参考：两张表都到齐
    assert freshness.stale_only(meta, today=pd.Timestamp("2026-11-03")) == []
    # 用 11-05 当参考：都落后了
    late = freshness.stale_only(meta, today=pd.Timestamp("2026-11-05"))
    assert len(late) == 2 and all(f.stale for f in late)


def test_freshness_respects_lag_days():
    """月频表在 lag 之内不该误报。"""
    import pandas as pd

    from causual.causual_core import freshness

    meta = pd.DataFrame([
        {"name": "cpi_yoy", "freq": "M", "lag_days": 9, "end_date": "2026-10-31"},
    ])
    # 11-05：上月末 10-31 + 9 天 = 11-09，还没到发布日 → 不报
    assert freshness.stale_only(meta, tables=("cpi_yoy",),
                                today=pd.Timestamp("2026-11-05")) == []
    # 12-15：11 月数据应已发布（11-30 + 9 天 = 12-09），但表里还停在 10-31 → 报
    stale = freshness.stale_only(meta, tables=("cpi_yoy",),
                                 today=pd.Timestamp("2026-12-15"))
    assert len(stale) == 1


def test_ops_page_renders_with_grouped_messages():
    """运维页有多条消息时按组渲染，不能 500。

    **用临时库**：早期版本直接往真实库里插测试消息，污染了运维页。
    """
    import shutil
    import tempfile

    import service.app as app_mod
    from fastapi.testclient import TestClient

    tmp = Path(tempfile.mkdtemp()) / "ops.sqlite"
    OpsStore(tmp)                     # 建表
    old = app_mod.DB_PATH
    app_mod.DB_PATH = tmp
    try:
        s = OpsStore(tmp)
        # 必须在**一次** save_result 里写多条：分开写的话后一次会作废前一次，
        # 这正是「重跑清旧消息」的语义。
        s.save_result(
            "validate", StateStatus.WAITING_ACK.value,
            messages=[{"id": f"m-grp{i}", "level": "warning",
                       "text": f"告警 {i}", "state": "validate",
                       "key": f"tbl{i}", "period": "2026-11-04",
                       "created_at": "2026-11-04T10:00:00"} for i in range(3)],
            blocking_msg_id="m-grp0")
        r = TestClient(app_mod.app).get("/ops")
        assert r.status_code == 200, r.status_code
        assert "展开全部" in r.text
        assert r.text.count("告警") >= 3
    finally:
        app_mod.DB_PATH = old
        shutil.rmtree(tmp.parent, ignore_errors=True)


def test_rerun_clears_stale_messages():
    """重跑后，上一轮的旧消息必须作废，不能一直挂在待办里。"""
    p = _tmp()
    store = OpsStore(p)

    # 上一轮：3 条不同 key 的消息
    old = [{"id": f"m-old{i}", "level": "warning", "text": f"旧告警 {i}",
            "state": "validate", "key": f"tbl{i}", "period": "2026-11-01",
            "created_at": "2026-11-01T10:00:00"} for i in range(3)]
    store.save_result("validate", StateStatus.WAITING_ACK.value,
                      messages=old, blocking_msg_id="m-old0")

    # 这一轮只产 1 条
    new = [{"id": "m-new", "level": "info", "text": "新的结果",
            "state": "validate", "created_at": "2026-11-02T10:00:00"}]
    store.save_result("validate", StateStatus.DONE.value,
                      messages=new, blocking_msg_id=None)

    unacked = [m["id"] for m in store.messages(unacked_only=True)
               if m["state"] == "validate"]
    assert unacked == ["m-new"], unacked
    assert len([m for m in store.messages() if m["state"] == "validate"]) == 4


def test_user_acked_message_survives_rerun():
    """用户确认过的消息不能被重跑冲掉。"""
    p = _tmp()
    store = OpsStore(p)
    store.save_result("validate", StateStatus.WAITING_ACK.value,
                      messages=[{"id": "m-u1", "level": "warning", "text": "x",
                                 "state": "validate", "key": "k",
                                 "created_at": "2026-11-01T10:00:00"}],
                      blocking_msg_id="m-u1")
    store.ack_message("m-u1", by="user", action="skip")
    store.save_result("validate", StateStatus.DONE.value,
                      messages=[{"id": "m-u2", "level": "info", "text": "y",
                                 "state": "validate", "created_at": "2026-11-02T10:00:00"}])
    acked = {m["id"]: m["ack_by"] for m in store.messages() if m["state"] == "validate"}
    assert acked["m-u1"] == "user", acked
