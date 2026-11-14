"""解耦约束：只有 CLI 与端口可以 import 其他模块。

模块内部（assets / core / web）必须只吃参数，不自己去拿数据。
这条测试是架构约束的可执行版本——比文档可靠。
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWED = {
    "causual/causual_core/cli.py",     # 命令行负责装配
    "state_layer/cli.py",
}
PATTERN = re.compile(r"^\s*(?:from|import)\s+(datahub|state_layer|causual|service)\b",
                     re.M)


def _violations(base: str, self_name: str) -> list[str]:
    out = []
    for p in (ROOT / base).rglob("*.py"):
        rel = p.relative_to(ROOT).as_posix()
        if rel in ALLOWED:
            continue
        text = p.read_text(encoding="utf-8")
        for m in PATTERN.finditer(text):
            mod = m.group(1)
            if mod == self_name:
                continue
            line = text[m.start():].lstrip().split("\n")[0]
            # 纯工具（shared.panel_align）不是数据访问，允许
            if mod == "datahub" and "align" in line:
                continue
            # 模块注册不是数据访问，允许
            if mod == "service" and "registry" in line:
                continue
            out.append(f"{rel}: {line.strip()}")
    return out


def test_causual_does_not_import_other_modules():
    bad = _violations("causual", "causual")
    assert not bad, "因果层内部不应 import 其他模块：\n" + "\n".join(bad)


def test_state_layer_does_not_import_other_modules():
    bad = _violations("state_layer", "state_layer")
    assert not bad, "状态层内部不应 import 其他模块：\n" + "\n".join(bad)
