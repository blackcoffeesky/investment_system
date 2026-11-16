"""分层静态检查：**核心代码不许自己取数**。

用户拍的规则（2026-11-16）：

> `series_map` / `get_data` 是数据层提供的取数接口，但**其他模块只能通过
> `shared.adapter` 声明自己要什么**，而且**只能在 cli / service / web 路由里调**。
> 其他模块的接口应该**通过参数接收 DataFrame**，不直接取数。

为什么必须是**静态检查**：违反方式是静默的——`state_view` 里偷偷 `get_data` 一下，
测试全绿、行为也对，只是分层破了。文档里的规则会漂（这两天已经证明），
只有可执行的检查拦得住。

允许调 `get_data` 的地方：

- `shared/adapter/datahub_adapter.py`（定义处）
- `*/cli.py`（模块的装配点）
- `*/web/routes.py`（页面入口，等价于该模块的 CLI）
- `service/**`（框架层本身就是装配层）

禁止：`*/port.py`、`*/pipeline.py`、`*/features.py`、`*/analysis/*.py`。
`tests/` 与 `experiment/` 不在检查范围（前者本来就该直接调，后者是试验场）。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 这些目录不参与检查：测试与试验场本来就允许直接取数
SKIP_PARTS = {".git", "task", "data", "__pycache__", ".venv", "node_modules",
              "tests", "experiment"}

#: 文件名本身就是"核心逻辑"的文件
FORBIDDEN_NAMES = {"port.py", "pipeline.py", "features.py"}


def _forbidden(rel: Path) -> bool:
    return rel.name in FORBIDDEN_NAMES or "analysis" in rel.parts


def _calls_get_data(path: Path) -> list[int]:
    """返回该文件里 `get_data(...)` 调用的行号（AST，不看字符串/注释）。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return []
    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = (fn.id if isinstance(fn, ast.Name)
                else fn.attr if isinstance(fn, ast.Attribute) else "")
        if name == "get_data":
            lines.append(node.lineno)
    return lines


def test_core_modules_never_call_get_data():
    offenders: list[str] = []
    for f in ROOT.rglob("*.py"):
        rel = f.relative_to(ROOT)
        if any(p in SKIP_PARTS for p in rel.parts):
            continue
        if not _forbidden(rel):
            continue
        for lineno in _calls_get_data(f):
            offenders.append(f"{rel.as_posix()}:{lineno}")

    assert not offenders, (
        "核心代码不许调 get_data——数据要通过参数传进来，取数只在 "
        "cli.py / web/routes.py / service/ 里做：\n  " + "\n  ".join(offenders))


def test_allowed_entry_points_do_define_or_call_it():
    """反向校验：检查器得是"活着"的——定义处与至少一个装配点确实存在。

    否则哪天 `get_data` 改名/被删，上面那条会**永远绿**（空集合遍历），
    等于哨兵睡死。
    """
    adapter = ROOT / "shared" / "adapter" / "datahub_adapter.py"
    assert "def get_data(" in adapter.read_text(encoding="utf-8"), (
        "adapter 里没有 get_data 的定义——检查器会失效")

    callers = [f for f in ROOT.rglob("*.py")
               if "service" in f.relative_to(ROOT).parts
               or f.name in ("cli.py", "routes.py")]
    assert any(_calls_get_data(f) for f in callers), (
        "没有任何装配点在调 get_data——要么改名了，要么装配断了")
