"""`store` 的 v2 产物：读写往返 / legacy 迁移 / **读路径不写盘**。

为什么单独一个文件：v2 引入"长表 + 多分组 + legacy 迁移"，这几条都是**格式契约**，
与模型无关，所以用临时目录直接测 `store`，不碰真实产物。
"""

from __future__ import annotations

import json
import pickle
import tempfile
from pathlib import Path

import pandas as pd

from state_layer import store

DATETIME = pd.to_datetime


class _TmpModelDir:
    """把 `store.MODEL_DIR` 指到临时目录（其余路径逻辑照常走）。"""

    def __enter__(self) -> Path:
        self._tmp = tempfile.TemporaryDirectory()
        self._old = store.MODEL_DIR
        store.MODEL_DIR = Path(self._tmp.name)
        return store.MODEL_DIR

    def __exit__(self, *exc) -> None:
        store.MODEL_DIR = self._old
        self._tmp.cleanup()


def _tree(d: Path) -> dict[str, int]:
    """目录下所有文件的 mtime_ns —— 读路径写盘会让它变。"""
    return {p.relative_to(d).as_posix(): p.stat().st_mtime_ns
            for p in sorted(d.rglob("*")) if p.is_file()}


def _groups_df() -> pd.DataFrame:
    return pd.DataFrame({
        "date": DATETIME(["2026-11-10", "2026-11-11", "2026-11-10", "2026-11-11"]),
        "group": ["short_price"] * 2 + ["short_vol"] * 2,
        "state": [0, 1, 2, 2],
        "label": ["trend_down", "range", "high", "high"],
        "p_max": [0.9, 0.8, 0.7, 0.6],
    })


def _spec() -> dict:
    return {
        "primary": "short_price",
        "groups": {
            "short_price": {"k": 3, "features": ["ret_5"],
                            "labels": {"0": "trend_down", "1": "range", "2": "trend_up"}},
            "short_vol": {"k": 3, "features": ["rv_5"],
                          "labels": {"0": "low", "1": "normal", "2": "high"}},
        },
    }


def _params() -> dict:
    return {"groups": {
        "short_price": {"model": {"kind": "thmm"}, "window": {"features": ["ret_5"]}},
        "short_vol": {"model": {"kind": "hmm"}, "window": {"features": ["rv_5"]}},
    }}


def test_v2_roundtrip():
    """写三件套 → 读回来：组名/primary/参数/长表/单组标签全对得上。"""
    with _TmpModelDir():
        store.save_estimate("ashare", spec=_spec(), params=_params(),
                            groups=_groups_df())
        assert store.has_v2("ashare")
        assert store.group_names("ashare") == ("short_price", "short_vol")

        meta = store.load_meta("ashare")
        assert meta["schema"] == store.SCHEMA_V2
        assert meta["asset"] == "ashare"
        assert meta["primary"] == "short_price"

        params = store.load_params("ashare")
        assert params["schema"] == store.SCHEMA_V2
        assert params["groups"]["short_vol"]["model"]["kind"] == "hmm"

        g = store.load_groups("ashare")
        assert list(g.columns) == ["date", "group", "state", "label", "p_max"]

        px = store.load_group_labels("ashare")            # primary
        assert list(px.index) == list(DATETIME(["2026-11-10", "2026-11-11"]))
        assert list(px) == ["trend_down", "range"]

        vol = store.load_group_labels("ashare", "short_vol")
        assert list(vol) == ["high", "high"]


def test_reads_never_write():
    """读一遍再读一遍，文件树必须逐字节级不变（mtime_ns）。"""
    with _TmpModelDir() as root:
        store.save_estimate("ashare", spec=_spec(), params=_params(),
                            groups=_groups_df())
        d = root / "ashare"
        before = _tree(d)
        store.has_v2("ashare")
        store.group_names("ashare")
        store.load_meta("ashare")
        store.load_params("ashare")
        store.load_groups("ashare")
        store.load_group_labels("ashare")
        assert _tree(d) == before, f"读路径改了文件树：{set(_tree(d)) ^ set(before)}"


def test_reads_do_not_migrate_legacy():
    """legacy 只读：读它不能把文件搬进 legacy/——搬迁是写路径的事。"""
    with _TmpModelDir() as root:
        d = root / "ashare"
        d.mkdir(parents=True)
        (d / "labels.csv").write_text("date,label\n2026-11-10,range\n", encoding="utf-8")
        (d / "current.json").write_text(json.dumps({"k": 3}), encoding="utf-8")
        (d / "chain.pkl").write_bytes(pickle.dumps({"chain": []}))
        before = _tree(d)

        assert store.has_v2("ashare") is False
        assert store.group_names("ashare") == ()
        assert list(store.load_labels("ashare")) == ["range"]   # 旧接口仍可读

        assert not (d / "legacy").exists(), "读路径触发了 legacy 迁移"
        assert _tree(d) == before


def test_save_migrates_legacy_and_keeps_it():
    """首次 v2 写入把旧四件套移进 legacy/，内容不变、不删；重复写幂等。"""
    with _TmpModelDir() as root:
        d = root / "ashare"
        d.mkdir(parents=True)
        (d / "labels.csv").write_text("date,label\n2026-11-10,range\n", encoding="utf-8")
        (d / "chain.pkl").write_bytes(pickle.dumps({"chain": ["old"]}))

        store.save_estimate("ashare", spec=_spec(), params=_params(),
                            groups=_groups_df())

        assert not (d / "labels.csv").exists() and not (d / "chain.pkl").exists()
        legacy = d / "legacy"
        assert (legacy / "labels.csv").read_text(encoding="utf-8").startswith("date,label")
        assert pickle.loads((legacy / "chain.pkl").read_bytes())["chain"] == ["old"]

        store.save_estimate("ashare", spec=_spec(), params=_params(),
                            groups=_groups_df())
        assert (legacy / "chain.pkl").exists(), "重复写入把 legacy 弄丢了"


def test_missing_v2_raises_with_hint():
    """没有 v2 时三个 loader 都要抛，且报错带"怎么生成"。"""
    with _TmpModelDir():
        for fn in (store.load_meta, store.load_params, store.load_groups):
            try:
                fn("ashare")
            except FileNotFoundError as e:
                assert "state_layer.cli regime" in str(e), str(e)
            else:
                raise AssertionError(f"{fn.__name__} 应该抛 FileNotFoundError")


def test_primary_must_be_in_groups():
    """primary 不在 groups 里 → 拒绝写入（别产出下游取不到的产物）。"""
    with _TmpModelDir():
        bad = _spec()
        bad["primary"] = "nope"
        try:
            store.save_estimate("ashare", spec=bad, params=_params(),
                                groups=_groups_df())
        except ValueError as e:
            assert "primary" in str(e)
        else:
            raise AssertionError("primary 不在 groups 里应拒绝写入")
