"""causual 模块的 Web 入口（路由 + 模板都在本目录）。"""

from pathlib import Path

from causual.web import fit, routes

TEMPLATES = Path(__file__).parent / "templates"


def register_module():
    """causual 对外只暴露一个注册函数，内部可挂多个路由组。"""
    routes.register_module()
    fit.register_module()
    return None


__all__ = ["register_module", "TEMPLATES", "routes", "fit"]
