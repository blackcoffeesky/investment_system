"""跨模块规则：**每个模块都要能被导入**。

2026-11-22 的教训：删掉 `shared/utils/calendar.py` 时，靠 `grep` 找引用漏了
`causual/causual_assets/gold/pipeline.py` 里一行**死 import**（导入了但从不调用）。
它不会让 pyright 报错（"导入了不用"不是类型错误），也不会让任何单元测试变红——
**只有真的去 import 那个模块才会炸**，而 gold 插件正是这样在运行期崩的。

所以这里补一条最小成本的网：把 `shared/` `datahub/` `state_layer/` `causual/` `service/`
下**每个模块**导入一遍。它抓的是"模块级错误"这一类：
缺依赖、循环导入、被删掉的符号、import 期就执行逻辑的副作用。

跳过 `.__main__`：那是 `python -m pkg` 的入口，**本来就该在执行时退出**。
"""

from __future__ import annotations

import importlib
import pkgutil

PACKAGES = ("shared", "datahub", "state_layer", "causual", "service")


def _all_modules() -> list[str]:
    names: list[str] = []
    for pkg in PACKAGES:
        try:
            mod = importlib.import_module(pkg)
        except Exception:  # noqa: BLE001  包本身导入失败会在测试主体里报
            continue
        for info in pkgutil.walk_packages(getattr(mod, "__path__", []),
                                          prefix=f"{pkg}."):
            if info.name.endswith(".__main__"):
                continue
            names.append(info.name)
    return sorted(set(names))


def test_every_module_imports() -> None:
    failures: list[str] = []
    for name in _all_modules():
        try:
            importlib.import_module(name)
        except SystemExit as e:  # import 期就 exit = 副作用，应当只在 __main__ 里
            failures.append(f"{name}: import 期触发 SystemExit({e.code})")
        except Exception as e:  # noqa: BLE001
            failures.append(f"{name}: {type(e).__name__}: {e}")

    assert not failures, (
        f"{len(failures)} 个模块无法导入（死 import / 缺依赖 / 循环导入）：\n"
        + "\n".join(f"  - {f}" for f in failures[:20]))


def test_no_reference_to_removed_shared_calendar() -> None:
    """`shared.utils.calendar` 已于 2026-11-22 删除；这里挡住它回流。

    直接 import 会失败，但要给出**可读的**失败原因，而不是一个 ModuleNotFoundError。
    """
    try:
        importlib.import_module("shared.utils.calendar")
    except ModuleNotFoundError:
        return
    raise AssertionError("shared.utils.calendar 又出现了——它在生产代码里没有使用者，"
                         "日期对齐请用 shared.utils.time_align")
