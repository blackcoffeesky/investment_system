"""因果模块的状态：生成新卡片。

数据层三个状态都过了，才轮到这里。目标日期取**真实数据最后一天**
（面板最后一行），不是外推日——卡片上每个数字都来自那天。
"""

from __future__ import annotations

from service.ops.base_state import BaseState
from service.ops.messages import Message, MessageLevel, StateResult

ASSETS = ("ashare", "gold")


class GenerateMacroState(BaseState):
    """月频卡：结构层 + 供需层。

    幂等按**月份**：`macro_YYYY-MM.md` 存在就跳过。
    "是否需要出月频卡"由 `daily._month_of()` 用宏观表的最新 end_date 决定，
    所以不需要额外记"上次运行日期"——文件本身就是记录。
    """

    name = "generate_macro"
    prefix = ("fetch", "validate")
    label = "生成月频卡"
    default_snooze = "days:1"

    def start(self, ctx) -> None:
        ctx.logger.info("[generate_macro] 检查本月宏观卡")

    def run(self, ctx) -> StateResult:
        cards = ctx.services["causual"]     # 缺端口直接抛 KeyError，启动期就该发现


        made, skipped, failed = [], [], []
        for asset in ASSETS:
            try:
                month = cards.macro_month(asset)
                if cards.exists(asset, month, kind="macro"):
                    skipped.append(f"{asset} {month}")
                    continue
                cards.generate_macro(asset, month)
                made.append(f"{asset} {month}")
            except Exception as e:  # noqa: BLE001
                failed.append((asset, f"{type(e).__name__}: {e}"))

        if not made and not skipped and failed:
            detail = "；".join(f"{a}: {m}" for a, m in failed)
            return StateResult.error(f"全部资产月频卡失败。{detail}", self.name)

        if failed:
            r = StateResult()
            for asset, msg in failed:
                r.messages.append(Message(
                    MessageLevel.WARNING, f"{asset} 月频卡失败：{msg}",
                    self.name, key=f"macro_{asset}"))
            if made or skipped:
                r.messages.append(Message(
                    MessageLevel.INFO,
                    f"已生成 {len(made)}、跳过 {len(skipped)}", self.name))
            return r

        parts = []
        if made:
            parts.append("生成 " + "、".join(made))
        if skipped:
            parts.append("已存在 " + "、".join(skipped))
        return StateResult.ok("；".join(parts) or "本月宏观卡无需更新", self.name)

    def end(self, ctx) -> None:
        ctx.logger.info("[generate_macro] 结束")


class GenerateCardsState(BaseState):
    name = "generate_cards"
    prefix = ("fetch", "validate")
    label = "生成新卡片"
    default_snooze = "days:1"

    def start(self, ctx) -> None:
        ctx.logger.info("[generate_cards] 检查是否需要出卡")

    def run(self, ctx) -> StateResult:
        cards = ctx.services["causual"]     # 缺端口直接抛 KeyError，启动期就该发现


        made, skipped, failed = [], [], []
        for asset in ASSETS:
            try:
                date = cards.last_data_date(asset, ctx.data)
            except Exception as e:  # noqa: BLE001
                failed.append((asset, f"取不到最后交易日：{type(e).__name__}: {e}"))
                continue

            try:
                if cards.exists(asset, date):
                    skipped.append(f"{asset} {date}")
                    continue
                cards.generate(asset, date, ds=ctx.data)
                made.append(f"{asset} {date}")
            except Exception as e:  # noqa: BLE001
                failed.append((asset, f"{type(e).__name__}: {e}"))

        if not made and not skipped and failed:
            detail = "；".join(f"{a}: {m}" for a, m in failed)
            return StateResult.error(f"全部资产出卡失败。{detail}", self.name)

        if failed:
            # 单资产失败不阻塞整个状态机——另一支还能用
            r = StateResult()
            for asset, msg in failed:
                r.messages.append(Message(
                    MessageLevel.WARNING, f"{asset} 出卡失败：{msg}",
                    self.name, key=asset))
            if made or skipped:
                r.messages.append(Message(
                    MessageLevel.INFO,
                    f"已生成 {len(made)} 张、跳过 {len(skipped)} 张", self.name))
            return r

        parts = []
        if made:
            parts.append("生成 " + "、".join(made))
        if skipped:
            parts.append("已存在 " + "、".join(skipped))
        return StateResult.ok("；".join(parts) or "没有需要出的卡", self.name)

    def end(self, ctx) -> None:
        ctx.logger.info("[generate_cards] 结束")
