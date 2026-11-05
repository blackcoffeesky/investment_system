"""数据层的三个状态：抓取 → 手工数据检查 → 完整性校验。

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

# 手工表期望更新周期（天）
EXPECTED_DAYS = {
    "cb_holdings": 100,          # WGC 季报，滞后约 45 天
    "gold_etf_holdings": 40,     # WGC ETF 月报
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


def overdue_tables(meta: pd.DataFrame, today: pd.Timestamp) -> list[dict]:
    """哪些手工表已经超过期望更新周期。

    返回结构化结果（table / end_date / gap），状态层再拼文案——
    因为静音要按 table 做 key、按 end_date 做 period。
    """
    out = []
    idx = meta.set_index("name") if "name" in meta.columns else meta
    for table, days in EXPECTED_DAYS.items():
        if table not in idx.index:
            continue
        end = idx.loc[table, "end_date"]
        if not end:
            out.append({"table": table, "end_date": "", "gap": None,
                        "text": f"{table} 无数据"})
            continue
        gap = (today - pd.Timestamp(end)).days
        if gap > days:
            out.append({"table": table, "end_date": str(end), "gap": gap,
                        "text": f"{table} 已 {gap} 天未更新（期望 ≤{days} 天）"})
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
        report = ctx.data_service.run_update()
        if report.success:
            return StateResult.ok(report.summary(), self.name)

        failed = [d for d in report.details if d.get("status") == "失败"]
        names = "；".join(f"{d['table']}: {str(d.get('msg', ''))[:60]}" for d in failed[:3])
        stale = stale_tables(ctx.data_service.metadata(),
                             ctx.data_service.covered_tables(),
                             pd.Timestamp.today().normalize())

        if stale:
            return StateResult.error(
                f"更新失败 {report.failed} 张表，且有 {len(stale)} 张关键表已过期："
                f"{'、'.join(stale[:3])}。失败源：{names}", self.name)

        # 个别源暂时不可用（限流、抖动），但数据仍在保鲜期内 → 可跳过
        return StateResult.warn(
            f"{report.failed} 个源暂时不可用（数据仍在保鲜期内，可跳过）：{names}",
            self.name, key="fetch_unavailable")

    def end(self, ctx) -> None:
        ctx.logger.info("[fetch] 结束")


class ManualCheckState(BaseState):
    name = "manual_check"
    prefix = ("fetch",)
    label = "手工数据逾期检查"
    # 逾期多半是"新一期还没发布"——按数据期静音，出新的自然恢复
    default_snooze = "period_change"

    def start(self, ctx) -> None:
        ctx.logger.info("[manual_check] 检查手工表")

    def run(self, ctx) -> StateResult:
        overdue = overdue_tables(ctx.data_service.metadata(),
                                 pd.Timestamp.today().normalize())
        if not overdue:
            return StateResult.ok("手工数据均在更新周期内", self.name)

        # 每张逾期表单独一条消息：静音按表做 key，按 end_date 做 period
        r = StateResult()
        for o in overdue:
            r.messages.append(Message(
                MessageLevel.WARNING, o["text"], self.name,
                key=o["table"], period=o["end_date"]))
        return r

    def end(self, ctx) -> None:
        ctx.logger.info("[manual_check] 结束")


class ValidateState(BaseState):
    name = "validate"
    prefix = ("fetch", "manual_check")
    label = "数据完整性检查"
    default_snooze = "days:7"        # 缺口不是每天都能修，一周看一次

    def start(self, ctx) -> None:
        ctx.logger.info("[validate] 开始检查")

    def run(self, ctx) -> StateResult:
        try:
            issues = ctx.data_service.check()
        except Exception as e:  # noqa: BLE001
            return StateResult.error(f"完整性检查无法执行：{type(e).__name__}: {e}", self.name)

        if issues is None or not len(issues):
            return StateResult.ok("全部通过", self.name)

        structural, recent = [], []
        for _, r in issues.iterrows():
            text = str(r.get("issues", ""))
            if "读取失败" in text or "空表" in text or "日历失败" in text:
                structural.append(f"{r['name']}：{text}")
            elif "近期" in text:
                recent.append(f"{r['name']}：{text}")

        if structural:
            return StateResult.error("数据不可用：" + "；".join(structural[:3]), self.name)
        if recent:
            return StateResult.warn(
                f"近期缺口 {len(recent)} 张表：" + "；".join(recent[:3]),
                self.name, key="recent_gaps")
        return StateResult.ok(
            f"无近期缺口；{len(issues)} 张表有历史缺口（已登记，仅提示）", self.name)

    def end(self, ctx) -> None:
        ctx.logger.info("[validate] 结束")
