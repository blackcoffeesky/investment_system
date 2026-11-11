"""causual 的 Web 层：页面路由 + 卡片渲染。

`__init__` 故意不 import routes——渲染是库代码也会用的（`pipeline.run` 要调它），
如果这里一 import 就把 fastapi 拖进来，库路径就被绑死在 web 框架上了。
用 `register_module()` 时才加载路由。
"""

from pathlib import Path

TEMPLATES = Path(__file__).parent / "templates"


def register_module():
    from causual.web.routes import register_module as _reg

    return _reg()


def __getattr__(name):
    if name in ("router",):
        from causual.web.routes import router

        return router
    raise AttributeError(name)


__all__ = ["register_module", "TEMPLATES"]
