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
LEGACY_FILES = ("current.json", "labels.csv", "chain.pkl", "hmm.pkl")


def asset_dir(asset: str) -> Path:
    return MODEL_DIR / asset


# ---------------------------------------------------------------- spec
def save_spec(asset: str, spec: dict) -> Path:
    d = asset_dir(asset)
    d.mkdir(parents=True, exist_ok=True)
    p = d / "current.json"
    p.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def load_spec(asset: str) -> dict:
    p = asset_dir(asset) / "current.json"
    if not p.exists():
        raise FileNotFoundError(f"没有 {asset} 的状态模型：{p}；先跑 state_layer.cli regime")
    spec = json.loads(p.read_text(encoding="utf-8"))
    return _migrate(spec, asset)


def _migrate(spec: dict, asset: str) -> dict:
    """旧的扁平 spec（k / features 在顶层）→ 新的三段式（model / data / metrics）。

    老文件是历史产物，不能要求用户重跑一遍拟合才能看图。
    """
    if "model" in spec and "data" in spec:
        return spec
    k = spec.get("k")
    features = list(spec.get("features") or [])
    fit_date = spec.get("fit_date")
    sig = spec.get("label_signature")
    return {
        "asset": asset,
        "model": {
            "kind": "hmm",
            "k": k,
            "version": f"hmm-k{k}-{sig}-{fit_date}",
        },
        "data": {
            "features": features,
            "fit_date": fit_date,
            "label_signature": sig,
            "used_labels": spec.get("used_labels", []),
            "fit_id": spec.get("fit_id"),
            # 卡片要的状态画像（每个标签的占比、持续、收益、波动）
            "profile": spec.get("profile", []),
        },
        "metrics": {
            "method": "bhattacharyya",
            "min": float(spec.get("min_bhattacharyya", 0.0)),
            "passed": float(spec.get("min_bhattacharyya", 0.0)) >= 0.4,
        },
    }


# ---------------------------------------------------------------- 标签
def save_labels(asset: str, labels: pd.Series) -> Path:
    d = asset_dir(asset)
    d.mkdir(parents=True, exist_ok=True)
    p = d / "labels.csv"
    pd.DataFrame({"date": labels.index, "label": labels.values}).to_csv(
        p, index=False, encoding="utf-8-sig")
    return p


def load_labels(asset: str) -> pd.Series:
    """（legacy）逐日标签。**只读**；新产物请用 `load_group_labels`。"""
    p = asset_dir(asset) / "labels.csv"
    if not p.exists():
        return pd.Series(dtype=str)
    df = pd.read_csv(p)
    idx = pd.DatetimeIndex(pd.to_datetime(df["date"]))
    vals = [str(x) if x == x else "" for x in df["label"]]
    out = pd.Series(vals, index=idx, name="label", dtype=object)
    kept = out[out != ""]
    return cast(pd.Series, kept.astype(str))


# ---------------------------------------------------------------- 模型参数
def save_model(asset: str, name: str, data: dict) -> Path:
    d = asset_dir(asset)
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    with p.open("wb") as f:
        pickle.dump(data, f, protocol=4)
    return p


def save_chain(asset: str, chain: list[dict]) -> Path:
    """保存整条模型链（顺序即处理次序）。"""
    return save_model(asset, "chain.pkl", {"chain": chain})


def load_chain(asset: str) -> list:
    """读模型链；没有 chain.pkl 时回退到旧的单模型 hmm.pkl。"""
    try:
        return load_model(asset, "chain.pkl")["chain"]
    except FileNotFoundError:
        return [load_model(asset, "hmm.pkl")]


def load_model(asset: str, name: str) -> dict:
    p = asset_dir(asset) / name
    if not p.exists():
        raise FileNotFoundError(f"没有 {asset} 的模型文件：{p}")
    with p.open("rb") as f:
        return pickle.load(f)


# ================================================================ v2（多分组）
def has_v2(asset: str) -> bool:
    """v2 产物齐不齐（spec.json + params.pkl 都在才算）。**只读**。"""
    d = asset_dir(asset)
    return (d / "spec.json").exists() and (d / "params.pkl").exists()


def _migrate_legacy(asset: str) -> Path | None:
    """把上一代四件套移进 `legacy/`（**只在写路径调用**，幂等）。

    读路径绝不做这件事：页面刷新不能改文件树。旧文件不删——它们是历史产物，
    排查"当时为什么这么标"要靠它们。
    """
    d = asset_dir(asset)
    todo = [n for n in LEGACY_FILES if (d / n).exists()]
    if not todo:
        return None
    legacy = d / "legacy"
    legacy.mkdir(parents=True, exist_ok=True)
    for name in todo:
        (d / name).replace(legacy / name)
    return legacy


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
    _migrate_legacy(asset)

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


def labels_any(asset: str, group: str | None = None) -> pd.Series:
    """**过渡期**读标签：v2 优先，没有 v2 回退 legacy `labels.csv`。

    回退只发生在"读标签"——推断/外推仍要求 v2（不造假的分组）。
    """
    if has_v2(asset):
        return load_group_labels(asset, group)
    return load_labels(asset)


def spec_any(asset: str) -> dict:
    """**过渡期**读 spec：v2 的 `spec.json` 原样返回，否则回退 legacy `current.json`。

    两者的字段布局不同，调用方按 `schema` 判断（`estimator-v2` 有 `groups`/`primary`，
    legacy 有 `model`/`data`/`metrics`）。
    """
    if has_v2(asset):
        return load_meta(asset)
    return load_spec(asset)

