"""state_layer 的 Web 入口。"""

from state_layer.web.routes import TEMPLATES, register_module, router

__all__ = ["register_module", "router", "TEMPLATES"]
