"""装配入口：把各模块登记进 kernel 注册表。

`service/app.py`（Web）和 `service/ops/manager.py`（CLI）都从这里拿模块，
避免两边各写一份 import 列表。幂等，重复调用无害。
"""

from __future__ import annotations

_LOADED = False


def load_modules() -> None:
    global _LOADED
    if _LOADED:
        return
    # 模块自己的 web 包负责一切：路由、模板、端口工厂
    from causual import web as causual_web
    from datahub import web as datahub_web

    causual_web.register_module()
    datahub_web.register_module()
    _LOADED = True


def loaded() -> bool:
    return _LOADED
