"""状态调度器。

职责边界：
- Manager 决定**谁可以跑**、调用 start/run/end、把消息转交消息层、把用户的确认
  反馈给对应状态
- Manager **不解释**消息内容；只按 level + action 决定"继续 / 停 / 重跑"
- 持久化全部走 `datahub.ops.OpsStore`（单事务落盘）

崩溃语义：进程在 running 时崩掉 → 重启后该状态变 **blocked** 并生成一条
`UNEXPECTED_INTERRUPT`（ERROR），**不自动重跑**——因为 run() 可能有副作用
（生成卡片、发通知），重跑等于重复执行。必须由用户 RETRY。

性能：`run()` 开头取一次状态快照，之后在内存里更新，不再每个状态都查库。
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from datahub.ops import OpsStore

from service.ops.actions import ALLOWED_ACTIONS, AckAction
from service.ops.base_state import BaseState
from kernel.context import Context, ServiceBag
from service.ops.messages import (PASSABLE_VALUES, Message, MessageLevel,
                                  StateResult, StateStatus)


class Manager:
    def __init__(self, states: list[BaseState], services=None,
                 db_path: Path | None = None):
        self.states = {s.name: s for s in states}
        self.order = [s.name for s in states]
        if services is None:
            from service.bootstrap import load_modules

            load_modules()
            services = ServiceBag(ports())
        self.services = services
        self.store = OpsStore(db_path)
        self.log = logging.getLogger("service.ops")
        self.run_id: str = ""
        self._snap: dict[str, dict] = {}
        
        self._refresh()      # 构造只读快照，不写库

    # ---------------- 快照
    def _refresh(self) -> dict[str, dict]:
        self._snap = self.store.load_all()
        return self._snap

    def _set(self, name: str, status: str, msg_id: str | None = None) -> None:
        row = self._snap.setdefault(name, {"state": name, "attempts": 0})
        row["status"] = status
        row["blocking_msg_id"] = msg_id

    # ---------------- 恢复
    def recover(self) -> None:
        """running → blocked + UNEXPECTED_INTERRUPT，不自动重跑。

        **只在启动和每次 run 开始时调用**——页面加载不该触发写库，
        否则后台任务在写时，打开 /ops 就会撞锁。
        """
        for name, row in self.store.load_all().items():
            if row["status"] != StateStatus.RUNNING.value:
                continue
            msg = Message(
                MessageLevel.ERROR,
                "上次运行在该状态执行中被中断（进程崩溃或被杀）。"
                "因为该状态可能有副作用，不会自动重跑——请检查后选择 RETRY。",
                state=name,
            )
            self.store.save_result(
                name, StateStatus.BLOCKED.value,
                messages=[msg.to_dict()],
                blocking_msg_id=msg.id,
                touch=False,
            )
            self.log.warning("状态 %s 上次中断，已置为 blocked（消息 %s）", name, msg.id)
        self._refresh()

    # ---------------- 查询
    def status(self) -> dict:
        snap = self._snap or self._refresh()
        out = {}
        for name in self.order:
            row = snap.get(name, {})
            out[name] = {
                "state": row.get("status", StateStatus.PENDING.value),
                "label": self.states[name].label,
                "last_run_at": row.get("last_run_at"),
                "attempts": row.get("attempts", 0),
                "last_attempts": row.get("last_attempts", 0),
                "blocking_msg_id": row.get("blocking_msg_id"),
            }
        return out

    def _passable(self, name: str) -> bool:
        row = self._snap.get(name)
        return bool(row) and row.get("status") in PASSABLE_VALUES

    def ready(self, name: str) -> bool:
        return all(self._passable(p) for p in self.states[name].prefix)

    # ---------------- 执行
    def run(self, only: list[str] | None = None) -> dict:
        self.recover()       # 上次崩溃遗留的 running 先折算成 blocked
        self.run_id = f"r-{uuid.uuid4().hex[:10]}"
        self.store.log_run_start(self.run_id)
        self._refresh()
        results = {"run_id": self.run_id}

        for name in self.order:
            if only and name not in only:
                continue
            st = self.states[name]

            if not self.ready(name):
                results[name] = {"status": "waiting_prefix"}
                continue

            row = self._snap.get(name, {})
            cur = row.get("status", StateStatus.PENDING.value)
            if cur == StateStatus.DONE.value:
                results[name] = {"status": "already_done"}
                continue
            if cur in (StateStatus.BLOCKED.value, StateStatus.WAITING_ACK.value):
                results[name] = {"status": cur, "msg_id": row.get("blocking_msg_id")}
                break

            results[name] = self._execute(st)
            if results[name]["status"] in ("blocked", "waiting_ack"):
                break

        ok = all(v.get("status") in ("done", "already_done", "skipped", "snoozed",
                                     "waiting_prefix")
                 for v in results.values() if isinstance(v, dict))
        blocked = any(v.get("status") in ("blocked", "waiting_ack")
                      for v in results.values() if isinstance(v, dict))
        status = "blocked" if blocked else ("ok" if ok else "partial")
        self.store.log_run_end(self.run_id, status,
                               ",".join(f"{k}={v.get('status')}"
                                        for k, v in results.items() if isinstance(v, dict)))
        return results

    def _execute(self, st: BaseState) -> dict:
        name = st.name
        # attempts 记的是"没跑顺的次数"：DONE/SNOOZED 不累加，避免每天正常跑也涨
        prev_attempts = int(self._snap.get(name, {}).get("attempts") or 0)
        # 只有"开始执行"才更新 last_run_at
        self.store.set_status(name, StateStatus.RUNNING.value,
                              attempts=prev_attempts, touch=True)
        self._set(name, StateStatus.RUNNING.value)

        ctx = Context(services=self.services, logger=self.log, state_name=name)
        try:
            st.start(ctx)
            result = st.run(ctx)
        except Exception as e:  # noqa: BLE001
            self.log.exception("状态 %s 抛异常", name)
            result = StateResult.error(f"{type(e).__name__}: {e}", name)
        finally:
            try:
                st.end(ctx)
            except Exception:  # noqa: BLE001
                self.log.exception("状态 %s 的 end() 出错", name)

        for m in result.messages:
            m.state = name
            m.run_id = self.run_id

        # 静音的 WARNING 与所有 INFO 都自动置为已读，不留在待办列表里
        snoozed_ids = set()
        for m in result.messages:
            if m.level == MessageLevel.WARNING and self._snoozed(name, m):
                snoozed_ids.add(m.id)
            elif m.level == MessageLevel.INFO:
                snoozed_ids.add(m.id)

        level = result.level
        if level == MessageLevel.ERROR:
            err = result.error_message
            self.store.save_result(
                name, StateStatus.BLOCKED.value,
                messages=[m.to_dict() for m in result.messages],
                blocking_msg_id=err.id if err else None, touch=False,
                attempts=prev_attempts + 1)
            self._set(name, StateStatus.BLOCKED.value, err.id if err else None)
            return {"status": "blocked", "msg_id": err.id if err else None,
                    "text": err.text if err else ""}

        if level == MessageLevel.WARNING:
            # **逐条**判静音：静音一张表不该把整个状态也静音
            warnings = [m for m in result.messages if m.level == MessageLevel.WARNING]
            live = [m for m in warnings if not self._snoozed(name, m)]
            if not live:
                note = Message(
                    MessageLevel.INFO,
                    f"{len(warnings)} 条告警在静音期内，不打断",
                    state=name, key="snoozed")
                note.run_id = self.run_id
                result.messages.append(note)
                self.store.save_result(
                    name, StateStatus.SNOOZED.value,
                    messages=[m.to_dict() for m in result.messages],
                    blocking_msg_id=None, touch=False)
                for mid in snoozed_ids:
                    self.store.ack_message(mid, by="system", action="snoozed")
                self._set(name, StateStatus.SNOOZED.value, None)
                return {"status": "snoozed", "snoozed": True,
                        "messages": [m.text for m in result.messages]}
            warn = live[0]
            self.store.save_result(
                name, StateStatus.WAITING_ACK.value,
                messages=[m.to_dict() for m in result.messages],
                blocking_msg_id=warn.id if warn else None, touch=False,
                attempts=prev_attempts + 1)
            self._set(name, StateStatus.WAITING_ACK.value, warn.id if warn else None)
            return {"status": "waiting_ack", "msg_id": warn.id if warn else None,
                    "text": warn.text if warn else ""}

        self.store.save_result(
            name, StateStatus.DONE.value,
            messages=[m.to_dict() for m in result.messages],
            blocking_msg_id=None, touch=False)
        self._set(name, StateStatus.DONE.value, None)
        return {"status": "done", "messages": [m.text for m in result.messages]}

    # ---------------- 静音
    def _snoozed(self, state: str, msg) -> bool:
        """这条警告是否还在静音期。

        - days 模式：到点就清掉，恢复提醒
        - period 模式：数据期变了（有新一期）就清掉，恢复提醒
        """
        for row in self.store.snoozes(state):
            if row["key"] != msg.key:
                continue
            if row["mode"] == "days":
                if row["remind_at"] and row["remind_at"] <= datetime.now().isoformat(timespec="seconds"):
                    self.store.clear_snooze(state, row["key"])
                    return False
                return True
            if row["mode"] == "period":
                if msg.period and row["period"] != msg.period:
                    self.store.clear_snooze(state, row["key"])
                    return False
                return True
        return False

    # ---------------- 确认
    def ack(self, msg_id: str, action: str = "retry", by: str = "user",
            remind_at: str | None = None) -> dict:
        """用户确认。规则见 `service.ops.actions.ALLOWED_ACTIONS`。

        注意：**INFO 只记录确认，不改状态**——它的状态早已是 DONE，
        任何改状态的操作都会让一个已完成的状态被重跑或被标成跳过。
        """
        try:
            act = AckAction(action)
        except ValueError:
            return {"ok": False, "reason": f"未知操作：{action}"}

        msgs = [m for m in self.store.messages() if m["id"] == msg_id]
        if not msgs:
            return {"ok": False, "reason": "消息不存在"}
        msg = msgs[0]
        level = msg["level"]

        allowed = ALLOWED_ACTIONS.get(level, set())
        if act not in allowed:
            return {"ok": False, "reason": f"{level.upper()} 不允许 {act.value}",
                    "allowed": sorted(a.value for a in allowed)}

        self.store.ack_message(msg_id, by=by, action=act.value)
        state = msg.get("state") or ""

        if level == MessageLevel.INFO.value:
            return {"ok": True, "state": state, "level": level,
                    "action": act.value, "result": "ack_only"}

        if act == AckAction.RETRY:
            self.store.set_status(state, StateStatus.PENDING.value, blocking_msg_id=None)
            self._set(state, StateStatus.PENDING.value, None)
            return {"ok": True, "state": state, "level": level,
                    "action": act.value, "result": "rerun"}

        if act == AckAction.ACK_ONLY:
            return {"ok": True, "state": state, "level": level,
                    "action": act.value, "result": "ack_only"}

        # SKIP：按 remind_at 决定"跳过多久"
        mode = self._parse_snooze(remind_at, state)
        if mode == "once":
            # 只跳过本次：不写静音记录，下次仍会重新检查并再提醒
            self.store.set_status(state, StateStatus.SNOOZED.value, blocking_msg_id=None)
            self._set(state, StateStatus.SNOOZED.value, None)
            return {"ok": True, "state": state, "level": level, "action": "skip",
                    "result": "skip", "snooze": "once"}

        # 带静音：检查照跑，只是不打断 → 状态记为 SNOOZED（下次仍会重新检查）
        self.store.set_status(state, StateStatus.SNOOZED.value, blocking_msg_id=None)
        self._set(state, StateStatus.SNOOZED.value, None)
        key = msg.get("key") or state
        if mode.startswith("days:"):
            n = int(mode.split(":")[1])
            until = (datetime.now() + timedelta(days=n)).isoformat(timespec="seconds")
            self.store.snooze(state, key, "days", remind_at=until)
            return {"ok": True, "state": state, "level": level, "action": "skip",
                    "result": "snooze", "snooze": mode, "remind_at": until}
        # period_change
        self.store.snooze(state, key, "period", period=msg.get("period") or "")
        return {"ok": True, "state": state, "level": level, "action": "skip",
                "result": "snooze", "snooze": "period_change",
                "until_period": msg.get("period") or ""}

    def _parse_snooze(self, remind_at: str | None, state: str) -> str:
        """归一化静音参数：once / days:N / period_change。

        没传就用该状态自己声明的默认值。
        """
        if not remind_at:
            return self.states[state].default_snooze if state in self.states else "once"
        v = str(remind_at).strip().lower()
        if v in ("once", "period_change"):
            return v
        if v.startswith("days:"):
            return v
        return "once"

    # ---------------- 整轮重跑
    def reset_all(self, force: bool = False) -> dict:
        """把所有"已结束"的状态打回 pending，准备整轮重跑。

        - 只重置 done / snoozed / pending；**不碰 blocked / waiting_ack**
          （那说明还有未确认的消息，重跑会绕过确认流程）
        - attempts 清零，原值记进 last_attempts
        - 不清静音：静音是用户偏好，重跑不该解除
        """
        self._refresh()
        stuck = [n for n in self.order
                 if self._snap.get(n, {}).get("status")
                 in (StateStatus.BLOCKED.value, StateStatus.WAITING_ACK.value)]
        if stuck and not force:
            return {"ok": False,
                    "reason": "还有未确认的消息，先去处理：" + "、".join(stuck)}

        reset = []
        for n in self.order:
            row = self._snap.get(n, {})
            st = row.get("status", StateStatus.PENDING.value)
            if st in (StateStatus.BLOCKED.value, StateStatus.WAITING_ACK.value):
                continue
            self.store.set_status(n, StateStatus.PENDING.value,
                                  blocking_msg_id=None, attempts=0,
                                  last_attempts=int(row.get("attempts") or 0))
            reset.append(n)
        self._refresh()      # 内存快照必须跟库对齐（attempts/last_attempts 都变了）
        return {"ok": True, "reset": reset}

    def all_settled(self) -> bool:
        """所有状态都 done / snoozed —— 界面此时显示"重跑"。"""
        self._refresh()
        return all(self._snap.get(n, {}).get("status")
                   in (StateStatus.DONE.value, StateStatus.SNOOZED.value)
                   for n in self.order)

    def has_unacked(self) -> bool:
        self._refresh()
        return any(self._snap.get(n, {}).get("status")
                   in (StateStatus.BLOCKED.value, StateStatus.WAITING_ACK.value)
                   for n in self.order)

    # ---------------- 单个状态重跑
    def reset(self, name: str) -> dict:
        """把某个状态强制打回 pending（与 ack 是两件事）。

        ack 是"回应一条消息"，reset 是"我要重跑这个状态"。
        """
        if name not in self.states:
            return {"ok": False, "reason": f"未知状态：{name}"}
        self.store.set_status(name, StateStatus.PENDING.value, blocking_msg_id=None)
        self._set(name, StateStatus.PENDING.value, None)
        return {"ok": True, "state": name, "status": "pending"}


def run_cli() -> int:
    import argparse

    from datahub import DB_PATH

    from kernel.registry import ports
    from service.bootstrap import load_modules
    from service.ops.states import STATES

    ap = argparse.ArgumentParser(prog="python -m service.ops.manager")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--ack", help="消息 id")
    ap.add_argument("--action", default="retry", choices=["retry", "skip", "ack"])
    ap.add_argument("--reset", help="把某状态强制打回 pending")
    ap.add_argument("--only")
    a = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    load_modules()
    mgr = Manager(STATES, ServiceBag(ports()), DB_PATH)

    if a.status:
        print(json.dumps(mgr.status(), ensure_ascii=False, indent=2))
        return 0
    if a.ack:
        print(json.dumps(mgr.ack(a.ack, a.action), ensure_ascii=False))
        return 0
    if a.reset:
        print(json.dumps(mgr.reset(a.reset), ensure_ascii=False))
        return 0
    res = mgr.run(only=[x.strip() for x in a.only.split(",")] if a.only else None)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
