"""datahub 模块的 Web 入口（表清单 + 手工数据录入）。"""

from pathlib import Path

from datahub.web.routes import register_module, router

TEMPLATES = Path(__file__).parent / "templates"

__all__ = ["register_module", "router", "TEMPLATES"]
