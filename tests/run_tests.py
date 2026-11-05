"""不依赖 pytest 的测试跑法（沙箱里装不了 pytest）。

    python tests/run_tests.py

文件本身仍是 pytest 兼容的：以后装了 pytest，`pytest tests` 同样能跑。
"""

from __future__ import annotations

import importlib.util
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load_module(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def main() -> int:
    files = sorted(Path(__file__).parent.glob("test_*.py"))
    total = passed = 0
    failures = []

    for f in files:
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
                print(f"  PASS  {f.name}::{name}")
            except Exception as e:  # noqa: BLE001
                failures.append((f.name, name, e))
                print(f"  FAIL  {f.name}::{name}  {type(e).__name__}: {e}")

    print(f"\n{passed}/{total} 通过")
    if failures:
        print("\n失败详情：")
        for fname, name, e in failures:
            print(f"\n[{fname}::{name}]")
            traceback.print_exception(type(e), e, e.__traceback__, limit=3)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
