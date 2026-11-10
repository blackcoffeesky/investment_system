"""diagnose 的 Web 入口。"""

from diagnose.web.routes import TEMPLATES, register_module, router

__all__ = ["register_module", "router", "TEMPLATES"]
