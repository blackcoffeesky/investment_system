"""产物读写：模型、spec、标签。

为什么单独一层
--------------
模型只管数组，不认文件；`port` 只管对外接口，不认路径。
把「存哪儿、什么格式」收在这里，将来换存储（比如进数据库）只改这一个文件。

产物目录：`state_layer/saved_models/{asset}/`

**v2（当前，多分组）**
    spec.json     {"schema":"estimator-v2", "primary", "groups":{name:{k,features,labels,fit_id,usable,signature,…}}}
    groups.csv    长表：date, group, state, label, p_max      ← 组数可变，所以用长表
    params.pkl    {"schema":"estimator-v2", "groups":{name:{"model": …, "window": …}}}

**legacy（上一代，只读）**
    current.json / labels.csv / chain.pkl / hmm.pkl
    → 首次 v2 写入时自动移进 `legacy/`，**不删**（删目录连带删产物的坑踩过）。

**读路径绝不写盘**：页面刷新、卡片生成只读，不能改文件树（测试用 mtime_ns 钉住）。
legacy → `legacy/` 的搬迁只发生在 `save_estimate()`（写路径）里。
"""

from __future__ import annotations

import json
import pickle
from pathlib import Path
from typing import cast

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
MODEL_DIR = ROOT / "state_layer" / "saved_models"

#: v2 产物的 schema 标记（写进 spec.json / params.pkl，方便以后迁移）
SCHEMA_V2 = "estimator-v2"

#: legacy 四件套（迁移时搬进 `legacy/`）


def asset_dir(asset: str) -> Path:
    return MODEL_DIR / asset


# ---------------------------------------------------------------- spec
def has_v2(asset: str) -> bool:
    """v2 产物齐不齐（spec.json + params.pkl 都在才算）。**只读**。"""
    d = asset_dir(asset)
    return (d / "spec.json").exists() and (d / "params.pkl").exists()


def save_estimate(asset: str, *, spec: dict, params: dict,
                  groups: pd.DataFrame) -> Path:
    """写 v2 三件套。写之前把旧产物移进 `legacy/`。

    - `spec`：`{schema?, primary, groups:{name:{…}}}`；`primary` 必须真的在 `groups` 里
    - `params`：`{groups:{name:{"model": …, "window": …}}, last_fit?}`（原样 pickle）
    - `groups`：长表（`state_assets.base.GROUP_COLUMNS`）
    """
    primary = spec.get("primary")
    names = set((spec.get("groups") or {}).keys())
    if not names:
        raise ValueError(f"{asset}: spec.groups 为空，拒绝写入")
    if primary not in names:
        raise ValueError(f"{asset}: primary={primary!r} 不在 groups {sorted(names)} 里")

    d = asset_dir(asset)
    d.mkdir(parents=True, exist_ok=True)

    meta = {**spec, "schema": SCHEMA_V2, "asset": asset}
    (d / "spec.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    groups.to_csv(d / "groups.csv", index=False, encoding="utf-8-sig")
    with (d / "params.pkl").open("wb") as f:
        pickle.dump({"schema": SCHEMA_V2, **params}, f, protocol=4)
    return d / "spec.json"


def load_meta(asset: str) -> dict:
    """读 v2 spec.json。没有就抛 `FileNotFoundError`（**不造假 1 组**）。"""
    p = asset_dir(asset) / "spec.json"
    if not p.exists():
        raise FileNotFoundError(
            f"没有 {asset} 的 v2 产物：{p}；先跑 "
            f"`python -m state_layer.cli regime --asset {asset}`")
    return json.loads(p.read_text(encoding="utf-8"))


def load_params(asset: str) -> dict:
    """读 v2 params.pkl（各组模型参数 + 最后窗口）。"""
    p = asset_dir(asset) / "params.pkl"
    if not p.exists():
        raise FileNotFoundError(
            f"没有 {asset} 的 v2 参数：{p}；先跑 "
            f"`python -m state_layer.cli regime --asset {asset}`")
    with p.open("rb") as f:
        return pickle.load(f)


def load_groups(asset: str) -> pd.DataFrame:
    """读 v2 groups.csv（长表）。"""
    p = asset_dir(asset) / "groups.csv"
    if not p.exists():
        raise FileNotFoundError(
            f"没有 {asset} 的 v2 分组标签：{p}；先跑 "
            f"`python -m state_layer.cli regime --asset {asset}`")
    df = pd.read_csv(p)
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"])
    return df


def group_names(asset: str) -> tuple[str, ...]:
    """v2 里有哪些组（按 spec 里的顺序）。没有 v2 返回空元组。"""
    try:
        meta = load_meta(asset)
    except FileNotFoundError:
        return ()
    return tuple((meta.get("groups") or {}).keys())


def series_of(groups: pd.DataFrame, group: str) -> pd.Series:
    """长表 → 某组的逐日标签（index=date，值=字符串标签）。纯格式转换。"""
    mask = groups["group"].to_numpy() == group
    sub = groups.loc[mask].sort_values(by=["date"], kind="stable")
    return pd.Series(sub["label"].to_numpy(),
                     index=pd.DatetimeIndex(sub["date"]),
                     name="label", dtype=object)


def load_group_labels(asset: str, group: str | None = None) -> pd.Series:
    """读 v2 某组的标签；`group=None` 取 spec 里的 `primary`。没有 v2 就抛。"""
    meta = load_meta(asset)
    names = (meta.get("groups") or {})
    g = group if group is not None else meta.get("primary")
    if not isinstance(g, str) or g not in names:
        raise KeyError(
            f"{asset} 没有组 {g!r}；可用：{sorted(names)}（primary={meta.get('primary')!r}）")
    return series_of(load_groups(asset), g)


