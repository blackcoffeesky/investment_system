"""`datahub.fin.update` 的命令行契约。

2026-11-15 的坑：`if __name__ == "__main__":` 块排在 `write_long` 定义**之前**。
`python -m datahub.fin.update --backfill <表>` 时主块先执行，
`backfill()` 的 `flush` 去调 `write_long` → `NameError`。
而 `--backfill` 是"先打 10 次接口再落库"，于是**100 次接口额度白烧、一行没写**。

`import` 时看不出来（模块会完整执行一遍），只有 `-m` 跑才炸。所以这两条：
1. 结构性断言：主块之后不允许再出现模块级 `def`
2. `-m ... --help` 冒烟：真的把模块当脚本跑一次（不碰库、不打接口）
"""

from __future__ import annotations

import inspect
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_main_block_is_last_definition():
    """`__main__` 块必须是文件末尾——它一执行就会调 `backfill`，而 `backfill` 要用
    后面定义的 `write_long` / `write_static`。"""
    import datahub.fin.update as U

    lines = inspect.getsource(U).splitlines()
    start = next((i for i, ln in enumerate(lines)
                  if ln.startswith('if __name__ == "__main__"')), None)
    assert start is not None, "没找到 `if __name__ == \"__main__\"` 块"

    later = [(i + 1, ln) for i, ln in enumerate(lines[start + 1:], start + 1)
             if ln.startswith("def ")]
    assert not later, (
        "主块之后还有模块级定义，`python -m datahub.fin.update` 会在它们定义之前"
        f"执行主块 → NameError。位置：{later[:3]}")


def test_module_runs_as_script():
    """把模块当脚本跑一次（`--help` 在解析参数时就退出，不读库、不打接口）。

    ⚠️ 必须显式 `encoding="utf-8"`：Windows 默认用 GBK 解码子进程输出，
    帮助文本里有非 GBK 字符 → 读线程抛 UnicodeDecodeError，`stdout` 变成 None
    （表现为 `TypeError: argument of type 'NoneType' is not iterable`）。
    """
    r = subprocess.run([sys.executable, "-m", "datahub.fin.update", "--help"],
                       cwd=str(ROOT), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=120)
    assert r.returncode == 0, f"--help 都跑不通：{(r.stderr or '').strip()[:400]}"
    assert "--backfill" in (r.stdout or ""), "帮助里没有 --backfill，参数定义是不是丢了？"
