"""卡片生成服务：状态机用它产出新卡。

为什么单独一个服务类而不是直接 import causual 的 pipeline：
- 状态层只认 `ctx.extra["cards"]`，不认 causual 的内部实现
- 将来换渲染方式（比如生成 HTML 直接入库）只改这里
"""

from __future__ import annotations

from pathlib import Path

from causual import causual_cards


class CardService:
    """生成/检查卡片。日期一律用**真实数据最后一天**。"""

    def __init__(self, output_dir: Path | None = None):
        self.output_dir = output_dir

    def last_data_date(self, asset: str, ds) -> str:
        """面板最后一天（真实交易日，不是外推日）。"""
        table = "csi_all" if asset == "ashare" else "gold_cny"
        s = ds.read(table)
        if s is None or not len(s):
            raise RuntimeError(f"{asset} 没有价格数据（{table} 为空）")
        return str(s.index[-1].date())

    def macro_month(self, asset: str) -> str:
        """月频卡应该覆盖的月份（宏观表最新 end_date 所在月）。"""
        from causual.causual_core.daily import _month_of

        return _month_of(asset)

    def generate_macro(self, asset: str, month: str) -> Path:
        """确保该月的宏观卡存在：生成一次即可（月频，比日频便宜）。"""
        path = causual_cards.card_path(asset, month, "macro")
        if path.exists():
            return path
        from causual.causual_core import daily

        daily.run(asset=asset, quiet=True)
        return causual_cards.card_path(asset, month, "macro")

    def exists(self, asset: str, date: str, kind: str = "card") -> bool:
        return causual_cards.card_path(asset, date, kind).exists()



    def regenerate(self, asset: str, date: str, ds=None) -> Path:
        """强制重生成某一天的卡（先删掉旧文件，保证一定重跑）。"""
        path = causual_cards.card_path(asset, date)
        if path.exists():
            path.unlink()
        return self.generate(asset, date, ds=ds)

    def generate(self, asset: str, date: str | None = None, ds=None) -> Path:
        """生成该资产在 `date` 的卡片；已存在则直接返回路径（幂等）。"""
        if date is None:
            if ds is None:
                raise ValueError("没有 date 时必须传 ds 用来取最后交易日")
            date = self.last_data_date(asset, ds)

        path = causual_cards.card_path(asset, date)
        if path.exists():
            return path

        from causual.causual_core import daily

        out = daily.run(asset=asset, asof=date, quiet=True, return_path=True)
        if not out or isinstance(out, int):
            raise RuntimeError(f"{asset} 卡片生成失败（返回 {out}）")
        return Path(out)
