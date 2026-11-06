"""datahub 的 Web 入口。"""

from datahub.web.routes import TEMPLATES, register_module, router

__all__ = ["register_module", "router", "TEMPLATES"]
