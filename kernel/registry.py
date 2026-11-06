"""模块注册表。

每个模块（`datahub` / `causual` / …）在 import 时把自己登记进来：
路由、模板目录、导航项，以及**它对其他模块暴露的能力**（service_port）。

service 只做两件事：把路由挂到 FastAPI 上、把 port 收进 `ServiceBag`。
它不认识任何模块的内部实现。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter


@dataclass
class Module:
    name: str
    label: str
    router: APIRouter
    template_dir: Path | None = None
    static_dir: Path | None = None
    nav: list[tuple[str, str]] = field(default_factory=list)
    # 对外暴露的能力。返回一个"端口"对象；模块没有对外能力就留 None。
    # service 在启动时统一调用一次，结果放进 Context.services。
    service_port_factory: Callable[[], Any] | None = None

    @property
    def has_port(self) -> bool:
        return self.service_port_factory is not None


_REGISTRY: list[Module] = []


def register(mod: Module) -> Module:
    _REGISTRY.append(mod)
    return mod


def all_modules() -> list[Module]:
    return list(_REGISTRY)


def template_dirs(service_dir: Path) -> list[str]:
    """Jinja2 的 searchpath：service 骨架优先，然后各模块。"""
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


def ports() -> dict[str, Any]:
    """构造所有模块的 port（每个 factory 只调一次）。"""
    out: dict[str, Any] = {}
    for m in _REGISTRY:
        if m.service_port_factory is None:
            continue
        out[m.name] = m.service_port_factory()
    return out
