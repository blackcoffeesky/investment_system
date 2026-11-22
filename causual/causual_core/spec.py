"""causual_core.spec —— 吃两种状态层 spec 结构（legacy 三段式 / estimator-v2 多分组）

为什么单独一个文件
----------------
状态层正在重构成"多分组"：`spec.json` 的 `label_signature` 落在
`spec["groups"][spec["primary"]]["label_signature"]`，而 legacy 的 `current.json`
在 `spec["data"]["label_signature"]`。causual 有 4 处要读签名、2 处要读画像——
把它们收在这里，别让"两种结构"散进各资产。

**按 `schema` 分支**，不要靠 try/except 猜结构：`KeyError` 做分支会把
"结构写错"和"结构是旧版"混成同一件事。
"""

from __future__ import annotations

import pandas as pd

#: v2 的 schema 标记（`state_layer/store.save_estimate` 写的）
V2_SCHEMA = "estimator-v2"


def is_v2(spec: dict | None) -> bool:
    return bool(spec) and spec.get("schema") == V2_SCHEMA


def primary_group(spec: dict | None) -> str | None:
    """v2 的 primary 分组名；legacy 没有分组概念，返回 None。"""
    if not is_v2(spec):
        return None
    assert spec is not None
    groups = spec.get("groups") or {}
    prim = spec.get("primary") or next(iter(groups), None)
    return str(prim) if prim is not None else None


def label_signature_of(spec: dict | None) -> str:
    """spec → 标签签名（用于和边快照比对）。

    v2：`spec["groups"][primary]["label_signature"]`
    legacy：`spec["data"]["label_signature"]`

    取不到就抛——签名缺失意味着"这套估计和那套标签对不上"，不能当成空串糊过去。
    """
    if not spec:
        raise KeyError("state['spec'] 是空的——装配点没把状态模型传进来？")
    if is_v2(spec):
        prim = primary_group(spec)
        try:
            return str(spec["groups"][prim]["label_signature"])
        except (KeyError, TypeError) as e:
            raise KeyError(
                f"v2 spec 里取不到 label_signature（primary={prim!r}，"
                f"groups={sorted((spec.get('groups') or {}))}）") from e
    try:
        return str(spec["data"]["label_signature"])
    except (KeyError, TypeError) as e:
        raise KeyError(f"legacy spec 里没有 data.label_signature：{sorted(spec)}") from e


def profile_of(spec: dict | None) -> pd.DataFrame | None:
    """legacy spec 自带的画像表（v2 **没有**，返回 None）。

    v2 的画像由状态层端口现算：`state_port.profile(asset, panel)`，装配点负责把它
    塞进 `state["profile"]`。这里只做"旧结构兼容"，不承担现算。
    """
    if not spec or is_v2(spec):
        return None
    data = spec.get("data") or {}
    rows = data.get("profile")
    if not rows:
        return None
    return pd.DataFrame(rows).set_index("label")
