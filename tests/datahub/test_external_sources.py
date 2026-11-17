"""外部网页源的"该响就响"契约（2026-11-17）。

事故：`world_tension` 的代码是

    if r.status_code == 200:
        ...            # 否则什么都不做

Cloudflare 从 2026-11-05 起拦 `python-requests` 默认 UA → 一直是 403 →
所有分段被静默跳过 → 最后只抛一句"没有返回任何数据"，**看不出是 403 还是真空**，
`world_tension` 就这么断了 12 天。

这跟 `INSERT OR REPLACE` 清空列、`fillna(0)` 伪造下跌是**同一类**：
**该响的地方没响**。所以这里用静态检查钉住三条：

1. 外部 GET 必须带浏览器 UA（Cloudflare 认 UA）
2. 不允许"只在 200 时做事、其余静默 continue"的写法
3. 非 200 必须抛（`_get` 里统一做）
"""

from __future__ import annotations

import inspect

from datahub.fin.sources import other


def test_browser_ua_is_defined_once():
    ua = other._BROWSER_UA
    assert ua.startswith("Mozilla/5.0"), f"UA 不像浏览器：{ua[:40]!r}"
    # 一个常量就够——别在各处复制一份（复制必然漂移）
    src = inspect.getsource(other)
    assert src.count("Mozilla/5.0") == 1, "浏览器 UA 出现了多次，应该只有 _BROWSER_UA 一处"


def _body(fn) -> str:
    """函数源码去掉 docstring 之后的部分（docstring 里会引用反例代码）。"""
    src = inspect.getsource(fn)
    end = src.find('"""', src.find('"""') + 3)
    return src[end + 3:] if end > 0 else src


def test_get_raises_on_non_200():
    """`_get` 必须把非 200 变成异常（而不是返回一个"空结果"）。"""
    body = _body(other._get)
    assert "!= 200" in body and "raise RuntimeError" in body
    assert "status_code == 200" not in body


def test_world_tension_no_longer_silently_skips():
    """曾经的写法 `if r.status_code == 200:` 必须消失——它会把 403 吞成'没有数据'。"""
    src = _body(other.world_tension)
    assert "status_code == 200" not in src, (
        "world_tension 又出现了'只在 200 时做事'的写法——403/超时会被静默吞掉")
    assert "_get(" in src, "world_tension 应走 _get（统一 UA + 非 200 抛错）"
