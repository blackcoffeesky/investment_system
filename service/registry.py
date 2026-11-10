"""模块注册表（service 侧）：模块把路由、模板、导航项、对外端口登记进来。

模块可以 import 这里的 `register` / `Module`（这是 service 的**公开接口**），
但不许 import service 的运维实现（manager / states / app）。

依赖方向因此是：`模块 → service.registry`，而 `service.app → 模块.web`。
两个方向都只经过 registry 这个薄壳，不互相牵引。
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
    # 对外暴露的能力；模块没有对外能力就留 None
    service_port_factory: Callable[[], Any] | None = None

    @property
    def has_port(self) -> bool:
        return self.service_port_factory is not None


_REGISTRY: list[Module] = []
_LOADED = False


def register(mod: Module) -> Module:
    _REGISTRY.append(mod)
    return mod


def load() -> None:
    """把各模块登记进来。幂等。"""
    global _LOADED
    if _LOADED:
        return
    from causual import web as causual_web
    from datahub import web as datahub_web
    from state_layer import web as state_web

    causual_web.register_module()
    datahub_web.register_module()
    state_web.register_module()
    _LOADED = True


def all_modules() -> list[Module]:
    load()
    return list(_REGISTRY)


def template_dirs(service_dir: Path) -> list[str]:
    dirs = [str(service_dir / "templates")]
    for m in all_modules():
        if m.template_dir:
            dirs.append(str(m.template_dir))
    return dirs


def nav_items() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = [("运维", "/ops")]
    for m in all_modules():
        out.extend(m.nav)
    return out


def ports() -> dict[str, Any]:
    """构造所有模块的 port（每个 factory 只调一次）。"""
    out: dict[str, Any] = {}
    for m in all_modules():
        if m.service_port_factory is not None:
            out[m.name] = m.service_port_factory()
    return out
