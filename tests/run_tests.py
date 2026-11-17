"""测试跑法（不依赖 pytest —— 沙箱里装不了）。

    python tests/run_tests.py                 # 全部（约 3 分钟）
    python tests/run_tests.py --fast          # 跳过 SLOW 标记（约 15 秒）
    python tests/run_tests.py datahub causual # 只跑指定模块
    python tests/run_tests.py --list          # 看有哪些组

目录按**模块**分：

    tests/shared/       耦合层 + 纯工具（adapter / 对齐 / 单位原语）
    tests/datahub/      数据层（写路径 / 派生注册表 / 窗口下推 / 新鲜度清单）
    tests/state_layer/  状态层（特征 / 预处理 / 模型契约）
    tests/causual/      因果层（面板 / 派生列 / 卡片 / 标签词表）
    tests/service/      框架层（状态机 / 端到端）
    tests/cross/        跨模块规则（解耦 / 分层）

文件本身仍是 pytest 兼容的：以后装了 pytest，`pytest tests` 同样能跑，
或者按组 `pytest tests/datahub`。

**新增测试放哪**：谁的功能，就放谁的目录；同时被两个模块守的规则放 `cross/`。
运行超过 10 秒的，在模块顶部写 `SLOW = True`，`--fast` 会跳过它。
"""

from __future__ import annotations

import importlib.util
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def groups() -> dict[str, list[Path]]:
    """组名（子目录）→ 测试文件，按名字排序。"""
    out: dict[str, list[Path]] = {}
    for d in sorted(p for p in TESTS.iterdir() if p.is_dir()):
        files = sorted(d.glob("test_*.py"))
        if files:
            out[d.name] = files
    loose = sorted(TESTS.glob("test_*.py"))     # 放错位置的文件也要跑，但会被告状
    if loose:
        out["(未归类)"] = loose
    return out


def load_module(path: Path):
    spec = importlib.util.spec_from_file_location(f"{path.parent.name}_{path.stem}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def main(argv: list[str]) -> int:
    want = [a for a in argv if not a.startswith("-")]
    fast = "--fast" in argv
    all_groups = groups()

    if "--list" in argv:
        for name, files in all_groups.items():
            n = sum(1 for f in files if not (fast and "SLOW = True" in f.read_text(encoding="utf-8")))
            print(f"  {name:14s} {n} 个文件")
        return 0

    if want:
        unknown = [w for w in want if w not in all_groups]
        if unknown:
            print(f"未知组：{unknown}；可用：{list(all_groups)}")
            return 2
        selected = {k: v for k, v in all_groups.items() if k in want}
    else:
        selected = all_groups

    total = passed = skipped = 0
    failures = []
    timings: list[tuple[float, str]] = []

    for group, files in selected.items():
        print(f"\n=== {group} ===")
        for f in files:
            if fast and "SLOW = True" in f.read_text(encoding="utf-8"):
                skipped += 1
                print(f"  SKIP  {f.name}（SLOW，--fast 跳过）")
                continue
            t0 = time.time()
            mod = load_module(f)
            for name in sorted(dir(mod)):
                if not name.startswith("test_"):
                    continue
                fn = getattr(mod, name)
                if not callable(fn):
                    continue
                total += 1
                try:
                    fn()
                    passed += 1
                except Exception as e:  # noqa: BLE001
                    failures.append((f"{group}/{f.name}", name, e))
                    print(f"  FAIL  {f.name}::{name}  {type(e).__name__}: {e}")
            timings.append((time.time() - t0, f"{group}/{f.name}"))

    slowest = sorted(timings, reverse=True)[:5]
    print("\n最慢的 5 个文件：")
    for dt, name in slowest:
        print(f"  {dt:6.1f}s  {name}")

    print(f"\n{passed}/{total} 通过" + (f"（跳过 {skipped} 个 SLOW 文件）" if skipped else ""))
    if failures:
        print("\n失败详情：")
        for fname, name, e in failures:
            print(f"\n[{fname}::{name}]")
            traceback.print_exception(type(e), e, e.__traceback__, limit=3)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
