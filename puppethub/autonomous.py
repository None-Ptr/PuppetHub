"""运行期自主 LLM：app 脱离对话自己跑「感知事件 → 决策 → 行动 → 记忆」循环。

核心主张（`docs/design-v2-autonomous.md`）：**自主不是"人不在场所以随便"，
而是"人不在场所以边界更硬"**。三条硬边界在这里落地：

1. **写者状态机**（`session.writer`）：llm / autonomous / none 互斥切换——
   单写者按实例算，自主当值时写者就是它，共作者退化为只读提问。
2. **默认拒绝**：危险能力与首次调用没有"当场确认"（没人在场），只能靠人
   **预先**写入自主白名单（`[autonomous] allow_calls`）；整体替换一律拒绝。
3. **熔断**：连续失败超过自主预算（比共作者更紧）→ 写者切回 none → 可见报警
   → 决策流水留痕 →（可选，配置开启）自动回滚到最近快照。

事件驱动优先于轮询：M2 的事件源是"引擎报错"（自我修复是最有价值、也最容易
验证的自主形态）。每次行动在 `autonomous.jsonl` 留结构化审计——"它当时为什么
这么做"必须查得回来，否则自主决策出错就成了悬案。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable, List, Optional

from .chat import Chat

DEFAULTS = {
    "fail_budget": 2,          # 共作者是 3；自主没人盯着，2 次就熔断
    "max_batch_lines": 40,     # 单批行数上限：大改动本来就该分段、可回滚
    "steps_per_hour": 60,      # 每小时步进上限：防"原地转圈刷预算"
    "auto_rollback": False,    # 熔断时自动回滚（默认关：回滚也是改动，要人点头）
    "allow_calls": [],         # 自主白名单：只能人预先声明，放宽必须显式确认
    "reflect_every": 20,       # 每 N 步插一步反思：把值得长期记住的沉淀进记忆（0=关）
}


class _LockedProvider:
    """同一 provider 实例被共作者与自主回路共用：**一次只许一个在流式**。

    两路并发打同一个上游不只是浪费——流式回调（on_delta）会把两路的输出
    混进同一条通道，人看到的就是乱码。
    """

    def __init__(self, inner, lock: threading.Lock):
        self._inner = inner
        self._lock = lock

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def stream(self, messages):
        with self._lock:
            yield from self._inner.stream(messages)


class AutonomousRunner:
    """自主回路的事件入口与预算执行者。决策-行动-观察**完全复用** `Chat`。"""

    def __init__(self, session, *, provider, prompts, storage, memory, log,
                 on_delta: Optional[Callable[[str], None]] = None, style=None):
        self.session = session
        self.log = log
        self.config = dict(DEFAULTS)
        self.config.update(session.config.get("autonomous") or {})
        self._llm_lock = threading.Lock()
        self.chat = Chat(
            session, provider=_LockedProvider(provider, self._llm_lock),
            prompts=prompts, storage=storage, memory=memory, log=log,
            on_delta=on_delta, origin="autonomous", style=style,
            fail_budget=int(self.config["fail_budget"]),
            autonomous_allow=list(self.config["allow_calls"]),
            max_batch_lines=int(self.config["max_batch_lines"]))
        self.busy = False
        self._lock = threading.Lock()
        self._steps: deque = deque(maxlen=int(self.config["steps_per_hour"]) + 8)
        self._since_reflect = 0                    # 距上次反思的步数（C：反思沉淀）
        self.started_at = ""

    # ------------------------------------------------------------ 预算

    def _budget_ok(self) -> str:
        window = time.time() - 3600
        recent = sum(1 for stamp in self._steps if stamp > window)
        limit = int(self.config["steps_per_hour"])
        if recent >= limit:
            return ("自主步进已达每小时预算（%d 次/小时）。挂起等待人调整——"
                    "不是静默丢弃" % limit)
        return ""

    # ------------------------------------------------------------ 步进

    def step(self, trigger: str) -> Optional[dict]:
        """跑一步自主循环。返回审计条目（被预算/忙拒时返回 None，但**可见**）。"""
        with self._lock:
            if self.busy:
                self.log("warning", "AUTONOMOUS_BUSY", "上一步还没跑完，本次触发被丢弃")
                return None
            reason = self._budget_ok()
            if reason:
                self.log("warning", "AUTONOMOUS_BUDGET", reason)
                return None
            self.busy = True
        try:
            return self._run(trigger)
        finally:
            with self._lock:
                self.busy = False

    def _run(self, trigger: str) -> dict:
        # **目标（C）**：自主从"被动响应"变"有方向"——目标存 storage 跨会话可见，
        # 由人或 LLM 用 goal 块设定。没有目标就是没有方向，prompt 里如实留空。
        goal = self.session.current_goal()
        request = ("【自主触发】%s\n\n"
                   "你是这个 app 的当值写者。%s\n"
                   "先判断这件事是否值得做：不值得就说明理由、什么都不改；值得就给出"
                   "最小的一步（命令批），并解释依据。危险动作与整体替换会被直接拒绝，"
                   "不要尝试。" % (trigger,
                                  ("你的当前目标：%s——触发事件若与目标无关，"
                                   "优先判断是否偏离方向。" % goal) if goal
                                  else "（当前没有设定目标。）"))
        result = self.chat.turn(request)
        self._steps.append(time.time())
        entry = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "trigger": trigger,
            "explanation": result.explanation[:400],
            "applied": result.applied,
            "skipped": result.skipped,
            "error": result.error,
            "diagnostics": len(result.diagnostics),
            "stuck": result.stuck,
        }
        self._audit(entry)
        if result.halted or self.chat.halted:
            self._circuit_break(trigger)
        else:
            self._maybe_reflect()
        return entry

    def _maybe_reflect(self) -> None:
        """反思（C）：每 N 步回顾一次，把值得长期记住的经验沉淀进记忆。

        反思本身也是一步（占预算、留审计）——"定期想"不能成为"无限想"的漏洞。
        """
        every = int(self.config.get("reflect_every") or 0)
        if not every:
            return
        self._since_reflect += 1
        if self._since_reflect < every:
            return
        self._since_reflect = 0
        recent = [item.get("trigger", "")[:60] for item in self.recent(8)]
        request = ("【反思】你刚连续自主运行了一段时间。回顾最近的步进"
                   "（%s）与你的记忆，把**值得长期记住**的经验用 remember 块沉淀"
                   "（重复的教训、已过时的记忆用 forget 清理）。没有值得沉淀的就"
                   "什么都不做——反思不是为产出而产出。" % "；".join(recent[-4:]))
        result = self.chat.turn(request)
        self._steps.append(time.time())
        self._audit({"time": time.strftime("%Y-%m-%d %H:%M:%S"),
                     "trigger": "反思",
                     "explanation": result.explanation[:400],
                     "applied": result.applied, "skipped": result.skipped,
                     "error": result.error, "diagnostics": len(result.diagnostics),
                     "stuck": result.stuck})

    def _audit(self, entry: dict) -> None:
        self.session.storage.append_jsonl(".puppethub/autonomous.jsonl", entry)

    # ------------------------------------------------------------ 熔断

    def _circuit_break(self, trigger: str) -> None:
        """连续失败到预算：写者切回 none，可见报警 + 决策流水 +（可选）自动回滚。"""
        self.log("error", "AUTONOMOUS_CIRCUIT",
                 "自主回路连续失败达到预算（%d 次），已熔断：写者切回「无人」。"
                 "恢复需要人显式交回。" % self.config["fail_budget"])
        self.session.set_writer("none", origin="autonomous",
                                reason="自主回路熔断（触发：%s）" % trigger[:120])
        self.session.app.append_decision(
            "自主回路熔断，写者切回无人",
            "连续失败 %d 次（触发：%s）" % (self.config["fail_budget"], trigger[:60]),
            "运行期")
        if self.config.get("auto_rollback"):
            snaps = [s for s in self.session.app.list_snapshots() if s.kind == "auto"]
            if snaps:
                self.session.restore(snaps[0].id)
                self.log("warning", "AUTONOMOUS_ROLLBACK",
                         "熔断后已自动回滚到最近快照 %s（auto_rollback 已开启）"
                         % snaps[0].id)

    # ------------------------------------------------------------ 事件源

    def on_diagnostic(self, level: str, code: str, message: str) -> None:
        """M2 事件源：引擎报错 → 自我修复。自己的动作产生的诊断不触发（那是回声）。"""
        if level != "错误" or self.session.writer != "autonomous" or self.busy:
            return
        # 撤销/拒绝类诊断是"动作的回声"，不是需要修复的故障——修它就是自我循环。
        if code in ("AUTONOMOUS_DENIED", "AUTONOMOUS_BUDGET", "AUTONOMOUS_CIRCUIT",
                    "CONFIRM_REQUIRED", "WRITER_READONLY", "LLM_HALTED"):
            return
        thread = threading.Thread(target=self.step,
                                  args=("引擎诊断 %s：%s" % (code, message[:160]),),
                                  daemon=True)
        thread.start()

    def on_peer_message(self, from_app: str, topic: str, message: str) -> None:
        """同胞消息（V4 A）：agent 社会的刺激源。已过写者/忙检查（投递端做）。"""
        thread = threading.Thread(
            target=self.step,
            args=("来自 app「%s」的消息（topic=%s）：%s"
                  % (from_app, topic, message[:200]),),
            daemon=True)
        thread.start()

    # ------------------------------------------------------------ 展示（M4）

    def recent(self, limit: int = 8) -> List[dict]:
        try:
            entries = self.session.storage.read_jsonl(".puppethub/autonomous.jsonl")
        except Exception:  # noqa: BLE001 - 审计文件读不出来要可见，但别炸面板
            self.log("error", "AUTONOMOUS_AUDIT", "自主审计文件读取失败")
            return []
        return entries[-limit:]

    def stats(self) -> dict:
        window = time.time() - 3600
        return {"writer": self.session.writer,
                "steps_last_hour": sum(1 for s in self._steps if s > window),
                "budget_per_hour": int(self.config["steps_per_hour"]),
                "allow": sorted(self.chat.autonomous_allow),
                "busy": self.busy}
