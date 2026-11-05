"""运维状态层：状态机持久化。

    from datahub.ops import OpsStore
    store = OpsStore()
    store.save_result("fetch", "blocked", messages=[...], blocking_msg_id="m-1")
"""

from datahub.ops.store import OpsStore

__all__ = ["OpsStore"]
