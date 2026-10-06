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

import json
import threading
import time
from collections import deque
from typing import Callable, List, Optional

from .chat import Chat


def _brief(value, limit: int = 40) -> str:
    """把一个状态值压成一行短文本（感知描述进 trigger，不能太长）。"""
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001 - 压不动就退回 repr，不因描述失败而丢掉感知
        text = repr(value)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _describe_changes(changes: list, limit: int = 5) -> str:
    """`[(类别, 地址, 之前, 之后), …]` → 一行可读描述。"""
    out = []
    for kind, addr, before, after in changes[:limit]:
        out.append("%s %s：%s → %s" % (kind, addr, _brief(before), _brief(after)))
    if len(changes) > limit:
        out.append("…共 %d 处" % len(changes))
    return "；".join(out)

DEFAULTS = {
    "fail_budget": 2,          # 共作者是 3；自主没人盯着，2 次就熔断
    "max_batch_lines": 40,     # 单批行数上限：大改动本来就该分段、可回滚
    "steps_per_hour": 60,      # 每小时步进上限：防"原地转圈刷预算"
    "auto_rollback": False,    # 熔断时自动回滚（默认关：回滚也是改动，要人点头）
    "allow_calls": [],         # 自主白名单：只能人预先声明，放宽必须显式确认
    "reflect_every": 20,       # 每 N 步插一步反思：把值得长期记住的沉淀进记忆（0=关）
    "perception_debounce": 1.5,  # 状态变化的合并窗口（秒）。盲区不走窗口（见 _perceive）
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
        # **事后评价（闭环的后半段）**：决策只通向"下一步"就永远学不到东西。
        # 每次决策把**机制可观测的后效**记在这里，下一步注入给它自己评价。
        # 证据由机制采集（诊断 / 熔断 / 产出 / 跳过），**不让 LLM 自报"我做对了"**。
        self._outcomes: deque = deque(maxlen=12)
        # 感知去抖：状态变化可能一帧来好几条，合并成一次唤醒（否则白烧预算）。
        self._perc_lock = threading.Lock()
        self._perc_pending: List[str] = []
        self._perc_timer: Optional[threading.Timer] = None

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
        # 身份（当值者 · 没有人在场）由 context 的【身份】块讲，**不在这里重复**——
        # 两处各讲一次就会变成"你是共作者"与"你是当值者"同时在场（提示词自相矛盾）。
        # 这里只交代**这一轮发生了什么**，并要求它先判断值不值得动。
        #
        # **带上上一步的后效**（闭环）：没有这一段，每一步都只看得到自己，
        # 于是"上次那么干出了错"这件事永远传不到下一次决策里。
        retro = self.retro_block()
        # **当前计划（规划层）**：让"下一步做哪件事"由计划回答，而不是每次重新判断。
        # 步骤完成状态**由机制按判据求值**（`session.plan_block`），不是问它自己。
        plan = self.session.plan_block() if hasattr(self.session, "plan_block") else ""
        request = ("【触发事件】%s\n\n"
                   "%s%s"
                   "这是你当值期间发生的一件事。先判断它是否值得你动："
                   "不值得就只说明理由、什么都不改（这是合法结果）；"
                   "值得就给最小的一步（命令批），并说清依据。"
                   "危险动作与整体替换会被直接拒绝，不要尝试。%s"
                   % (trigger,
                      (retro + "\n\n") if retro else "",
                      (plan + "\n\n") if plan else "",
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
        # **后效证据由机制记，不由它自报**（见 `_record_outcome`）。
        self._record_outcome(entry, result)
        if result.halted or self.chat.halted:
            self._circuit_break(trigger)
        else:
            self._maybe_reflect()
        return entry

    # ------------------------------------------------------------ 事后评价

    def _record_outcome(self, entry: dict, result) -> None:
        """记下这一步的**可观测后效**——评价的依据必须是证据，不是自我感觉。

        为什么必须机制采证：让 LLM 自己回答"上一步做得对吗"，它会答"对"——
        那不是评价，是自我肯定。真正能当证据的只有这几样**别人也能看见**的东西：
        改了没有 · 有没有被拒 · 有没有报错 · 有没有卡住。
        """
        codes = [str(d.get("code", "")) for d in (result.diagnostics or [])
                 if isinstance(d, dict) and d.get("level") == "error"]
        self._outcomes.append({
            "time": entry["time"],
            "trigger": entry["trigger"][:120],
            # 「什么都没改」是**合法且被鼓励**的结果，但它与"改了但被拒"要分得开——
            # 前者是判断力，后者是被拦下，混成一句"没动"就永远学不到东西。
            "acted": bool(result.applied),
            "refused": [s[:120] for s in (result.skipped or [])
                        if "拒绝" in s or "被拒" in s],
            "errors": sorted(set(c for c in codes if c))[:5],
            "stuck": bool(result.stuck or result.halted),
        })

    def retro_block(self, limit: int = 4) -> str:
        """给下一轮注入的「上一步到底怎么样」。空 = 没有可评价的历史。"""
        with self._lock:
            items = list(self._outcomes)[-limit:]
        if not items:
            return ""
        lines = []
        for item in items:
            if item["acted"] and not item["errors"] and not item["refused"]:
                verdict = "改了，且无报错"
            elif item["acted"] and item["errors"]:
                verdict = "改了，但**报了错**：%s" % "、".join(item["errors"])
            elif item["refused"]:
                verdict = "**被拒**（%s）" % "；".join(item["refused"])[:100]
            elif item["stuck"]:
                verdict = "**卡住了**（同一诊断重复或连续失败）"
            else:
                verdict = "判断后**决定不改**（合法结果）"
            lines.append("- %s｜%s → %s" % (item["time"], item["trigger"], verdict))
        return ("【上一步的后效（机制采证，不是你的自我评价）】\n"
                + "\n".join(lines)
                + "\n下一次动手前先看这里：**同类问题别再犯第二次**。")

    def _maybe_reflect(self) -> None:
        """反思（C）：每 N 步回顾一次，把值得长期记住的**教训**沉淀进记忆。

        反思本身也是一步（占预算、留审计）——"定期想"不能成为"无限想"的漏洞。
        它与 `retro_block` 的分工：后者每步都带（短的、一步之内的后效），
        这里做长回顾（"连着几步看下来，我这个 app 的什么认识该改"）。
        """
        every = int(self.config.get("reflect_every") or 0)
        if not every:
            return
        self._since_reflect += 1
        if self._since_reflect < every:
            return
        self._since_reflect = 0
        recent = [item.get("trigger", "")[:60] for item in self.recent(8)]
        retro = self.retro_block(limit=8) or "（没有可评价的历史）"
        request = ("【反思】你刚连续自主运行了一段时间。回顾最近的步进"
                   "（%s）与你的记忆，把**值得长期记住**的经验用 remember 块沉淀"
                   "（重复的教训、已过时的记忆用 forget 清理）。没有值得沉淀的就"
                   "什么都不做——反思不是为产出而产出。\n\n"
                   "%s" % ("；".join(recent[-4:]), retro))
        result = self.chat.turn(request)
        self._steps.append(time.time())
        self._record_outcome({"time": time.strftime("%Y-%m-%d %H:%M:%S"),
                              "trigger": "反思"}, result)
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

    # ---- 感知类（L2：宿主把"发生了什么"递进来）----

    def on_interaction(self, target: str, event: str) -> None:
        """**盲区**：用户点了/改了，但程序里既无处理器也无订阅 → 界面上什么也不会发生。

        不走去抖窗口：它是**明确的、一次性的**人为动作，延迟会让"点了没反应"更迟钝。
        """
        self._perceive("交互无处可去", "#%s %s" % (target, event), immediate=True)

    def on_state_change(self, changes: list) -> None:
        """**状态变化**：`data` / `slots` / `flags` 相对上一帧变了。

        走去抖窗口：一次操作常连带改好几样（数据 + 槽 + 标志），逐条唤醒是纯烧钱。
        """
        self._perceive("状态变化", _describe_changes(changes))

    def _perceive(self, kind: str, detail: str, immediate: bool = False) -> None:
        """感知汇入去抖器 → 唤醒决策层。

        **只有自主当值才感知**：共作者当值时是人在驱动，agent 不该插嘴。
        被 `step()` 拒掉（忙 / 超预算）时**可见**——那条可见性由 `step` 自己保证。
        """
        if self.session.writer != "autonomous":
            return
        item = "%s：%s" % (kind, detail)
        if immediate:
            self._run_perception([item])
            return
        delay = float(self.config.get("perception_debounce") or 0)
        if delay <= 0:
            with self._perc_lock:
                pending, self._perc_pending = self._perc_pending + [item], []
            self._run_perception(pending)
            return
        with self._perc_lock:
            self._perc_pending.append(item)
            if self._perc_timer is not None:
                return                     # 窗口已经在等，合并进去
            self._perc_timer = threading.Timer(delay, self._flush_perception)
            self._perc_timer.daemon = True
            self._perc_timer.start()

    def _flush_perception(self) -> None:
        with self._perc_lock:
            pending, self._perc_pending = self._perc_pending, []
            self._perc_timer = None
        if pending:
            self._run_perception(pending)

    def _run_perception(self, items: List[str]) -> None:
        """把攒下的感知拼成**一条** trigger 走正常的 step（预算/熔断/审计全都复用）。"""
        if not items:
            return
        shown = "；".join(items[:5])
        if len(items) > 5:
            shown += "；…共 %d 条" % len(items)
        self.step("【感知】%s" % shown)

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
