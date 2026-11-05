"""模块注册表：每个模块把自己的路由、模板、导航项登记到这里。

服务层只负责"发现 + 挂载 + 统一中间件"，不 import 任何模块的内部实现。

模块的 `register()` 约定：
```python
def register() -> Module:
    return Module(
        name="causual",
        label="卡片",
        router=router,                    # FastAPI APIRouter
        template_dir=Path(__file__).parent / "templates",
        nav=[("卡片", "/cards")],
    )
```
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from fastapi import APIRouter


@dataclass
class Module:
    name: str
    label: str
    router: APIRouter
    template_dir: Path | None = None
    static_dir: Path | None = None
    nav: list[tuple[str, str]] = field(default_factory=list)


_REGISTRY: list[Module] = []


def register(mod: Module) -> Module:
    _REGISTRY.append(mod)
    return mod


def all_modules() -> list[Module]:
    return list(_REGISTRY)


def template_dirs(service_dir: Path) -> list[str]:
    """Jinja2 的 searchpath：服务层自己的模板优先，然后各模块的。"""
    dirs = [str(service_dir / "templates")]
    for m in _REGISTRY:
        if m.template_dir:
            dirs.append(str(m.template_dir))
    return dirs


def nav_items() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = [("运维", "/ops")]
    for m in _REGISTRY:
        out.extend(m.nav)
    return out
