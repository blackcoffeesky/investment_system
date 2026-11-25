"""数据层的两个状态：抓取 → 数据检查（手工表逾期 + 日期缺口）。

一个模块一个文件：同一模块的状态共享常量与判断逻辑，放在一起才不会散。
真正的判断抽成**纯函数**（`stale_tables` / `overdue_tables`），方便单测；
状态类只负责 start/run/end 的编排。
"""

from __future__ import annotations

import pandas as pd

from service.ops.base_state import BaseState
from service.ops.messages import Message, MessageLevel, StateResult

# ---------------------------------------------------------------- 常量
# 保鲜期（天）：超过这个天数没更新，就认为该表"不可用"
FRESH_DAYS = {"D": 10, "W": 21, "M": 62, "Q": 150, "Y": 400}

# 每次抓数回溯多少天。建库时用 CLI 的 --full 或手动传更长的 start。
FETCH_LOOKBACK_DAYS = 400

# 手工表期望更新周期（天）——按**表自身频率**而不是统一天数。
# 季报按季、月报按月，否则 cb_holdings 每天都会报"逾期"。
EXPECTED_BY_FREQ = {"D": 5, "W": 14, "M": 45, "Q": 120, "Y": 400}

# 个别表手工指定（覆盖频率推算）
EXPECTED_DAYS = {
    "cb_holdings": 120,          # WGC 季报，滞后约 45 天
    "gold_etf_holdings": 45,     # WGC ETF 月报
    "insider_net": 45,           # 东财月度导出
    "equity_fin_monthly": 45,    # 人行社融表
}


# ---------------------------------------------------------------- 纯函数
def stale_tables(meta: pd.DataFrame, covered: set[str],
                 today: pd.Timestamp) -> list[str]:
    """哪些表已超出保鲜期。

    只看**启用源覆盖的表**——已停用的源（如 industrial_yoy）长期不动是已知状态。
    """
    out = []
    for _, r in meta.iterrows():
        name = str(r["name"])
        if name not in covered:
            continue
        end = r.get("end_date")
        if not end:
            out.append(name)
            continue
        limit = FRESH_DAYS.get(str(r.get("freq") or "D"), 62)
        if (today - pd.Timestamp(end)).days > limit:
            out.append(name)
    return out


def _intake_hint(table: str) -> str:
    """手工导入表返回"怎么更新"的一句话；不是手工表就返回空串。

    数据层的 intake 注册表是唯一真相——这里只做一处转手，别在 service 复制文案。
    """
    try:
        from datahub.fin.intake.registry import hint_for
    except Exception:  # noqa: BLE001  数据层没这个接口时不要让 ops 崩
        return ""
    try:
        return hint_for(table) or ""
    except Exception:  # noqa: BLE001
        return ""


def overdue_tables(meta: pd.DataFrame, today: pd.Timestamp) -> list[dict]:
    """哪些手工表已经超过期望更新周期。

    返回结构化结果（table / end_date / gap），状态层再拼文案——
    因为静音要按 table 做 key、按 end_date 做 period。
    """
    out = []
    idx = meta.set_index("name") if "name" in meta.columns else meta
    for table in EXPECTED_DAYS:
        if table not in idx.index:
            continue
        row = idx.loc[table]
        freq = str(row.get("freq") or "M")
        days = EXPECTED_DAYS.get(table) or EXPECTED_BY_FREQ.get(freq, 45)
        end = row.get("end_date")
        if not end:
            out.append({"table": table, "end_date": "", "gap": None,
                        "text": f"{table} 无数据"})
            continue
        gap = (today - pd.Timestamp(end)).days
        if gap > days:
            # 手工导入表（WGC 月报等）要给出**可执行**的更新方式，
            # 否则用户只能去翻代码才知道这张表根本不归 update 管。
            text = f"{table} 已 {gap} 天未更新（期望 ≤{days} 天）"
            hint = _intake_hint(table)
            if hint:
                text += f"（{hint}）"
            out.append({"table": table, "end_date": str(end), "gap": gap,
                        "text": text})
    return out


# ---------------------------------------------------------------- 状态
class FetchState(BaseState):
    name = "fetch"
    prefix: tuple[str, ...] = ()
    label = "抓取最新数据"
    default_snooze = "days:1"        # 源暂时不可用，明天再看

    def start(self, ctx) -> None:
        ctx.logger.info("[fetch] 开始拉取全部启用源")

    def run(self, ctx) -> StateResult:
        report = ctx.data.run_update()
        if report.success:
            return StateResult.ok(report.summary(), self.name)

        failed = [d for d in report.details if d.get("status") == "失败"]
        names = "；".join(f"{d['table']}: {str(d.get('msg', ''))[:60]}" for d in failed[:3])
        stale = stale_tables(ctx.data.metadata(),
                             ctx.data.covered_tables(),
                             pd.Timestamp.today().normalize())

        # 2026-11-25：区分"网络抖动"与"结构性故障"。
        # 全是 transient（FRED 之类连不上，且已重试 2 次）→ warn，不阻塞；
        # 只要有一条 structural（字段变了、解析失败）→ 仍然 error，别让真故障被吞掉。
        if stale and not getattr(report, "all_transient", False):
            return StateResult.error(
                f"更新失败 {report.failed} 张表，且有 {len(stale)} 张关键表已过期："
                f"{'、'.join(stale[:3])}。失败源：{names}", self.name)

        if stale:
            return StateResult.warn(
                f"更新失败 {report.failed} 张表（均为临时网络问题，已自动重试 2 次），"
                f"其中 {len(stale)} 张已过期：{'、'.join(stale[:3])}。失败源：{names}",
                self.name, key="fetch_transient", blocking=False)

        # 个别源暂时不可用（限流、抖动），但数据仍在保鲜期内 → 可跳过
        return StateResult.warn(
            f"{report.failed} 个源暂时不可用（数据仍在保鲜期内）：{names}",
            self.name, key="fetch_unavailable", blocking=False)

    def end(self, ctx) -> None:
        ctx.logger.info("[fetch] 结束")


class ValidateState(BaseState):
    """数据检查：手工表逾期 + 日期缺口。

    原先 manual_check / validate 是两个状态，但两者产出同一种 WARNING、
    用户动作也一样（补数据 / 忽略），却要确认两次——合并成一个。
    消息 key 仍是表名，静音粒度不变。
    """

    name = "validate"
    prefix = ("fetch",)
    label = "数据检查（手工表 + 缺口）"
    # 逾期多半是"新一期还没发布"——按数据期静音，出新的自然恢复
    default_snooze = "period_change"

    def start(self, ctx) -> None:
        ctx.logger.info("[validate] 检查手工表与日期缺口")

    def run(self, ctx) -> StateResult:
        r = StateResult()

        # ① 手工表逾期：每张表一条消息（key=表名，period=end_date）
        for o in overdue_tables(ctx.data.metadata(),
                                pd.Timestamp.today().normalize()):
            r.messages.append(Message(
                MessageLevel.WARNING, o["text"], self.name,
                key=o["table"], period=o["end_date"]))

        # ② 日期缺口
        try:
            issues = ctx.data.check()
        except Exception as e:  # noqa: BLE001
            r.messages.append(Message(
                MessageLevel.ERROR,
                f"完整性检查无法执行：{type(e).__name__}: {e}", self.name))
            return r

        if issues is None or not len(issues):
            if not r.messages:
                return StateResult.ok("手工表与日期缺口均正常", self.name)
            return r

        structural, recent = [], []
        for _, row in issues.iterrows():
            text = str(row.get("issues", ""))
            if "读取失败" in text or "空表" in text or "日历失败" in text:
                structural.append(f"{row['name']}：{text}")
            elif "近期" in text:
                recent.append(f"{row['name']}：{text}")

        if structural:
            r.messages.append(Message(
                MessageLevel.ERROR, "数据不可用：" + "；".join(structural[:3]),
                self.name, key="structural"))
            return r

        if recent:
            r.messages.append(Message(
                MessageLevel.WARNING,
                f"近期缺口 {len(recent)} 张表：" + "；".join(recent[:3]),
                self.name, key="recent_gaps"))

        if not r.messages:
            return StateResult.ok(
                f"无近期缺口；{len(issues)} 张表有历史缺口（已登记，仅提示）", self.name)
        return r

    def end(self, ctx) -> None:
        ctx.logger.info("[validate] 结束")
