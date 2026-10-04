"""共作者 LLM 的对话回路：输入 → 上下文 → 流式 → 指令块 → 执行 → **诊断回灌**。

三条把"搭建回路"闭合起来的东西：

1. **LLM 输出指令块，不重写文件**。改程序只走命令批（带校验、行号诊断、批末原子写回、
   自动快照），与人走同一条路。命令批之外还有三种块：受限文件写入（`capabilities.py` /
   留 `assets/**` / `DESIGN.md`）、整体替换（需人确认）、反问（反问期不写入）。
2. **诊断回灌**。引擎的行号诊断进下一轮上下文——回路不闭合，搭建就退化成盲写。
3. **卡住检测是可外部观测的信号**：同一诊断（码 + 消息）在最近两轮里重复出现 → 判定卡住，
   停止自动重试、换一种表述、连续失败到硬预算就交给人。判定不外包给 LLM 自己——
   陷在循环里的那一方往往不自知。

**零静默**：解析不出的块、讨论模式下被跳过的块、被确认拦下的块，全部记进结果与观察流。
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from puppet import Diagnostic, ERROR, INFO, WARNING
from puppet.lang import ActionStmt, CallStmt, On, parse_program

# 保留并注入的最近轮数：与 `context.HISTORY_K` 同值（文件与上下文必须一致）。
HISTORY_K = 12

# 能力名关键字：命中即需确认（**醒目层**）。靠命名猜一定会漏——所以下面还有兜底层。
DANGER_WORDS = ("del", "delete", "remove", "drop", "send", "pay", "publish", "post",
                "upload", "purchase", "transfer", "exec", "shell", "sudo", "wipe",
                "purge", "shutdown", "kill")

# 硬预算：同一目标连续失败超过这个次数就停手交给人。
FAIL_BUDGET = 3

FENCE = re.compile(r"^\s*```(.*)$")


class BlockError(RuntimeError):
    """受限文件写入越界等。必须可见。"""


# ------------------------------------------------------------------ 解析

@dataclass
class Block:
    kind: str                    # batch | replace | write | ask | unknown
    body: str = ""
    path: str = ""               # 仅 write
    info: str = ""


def parse_response(text: str) -> Tuple[List[Block], List[Diagnostic]]:
    """把 LLM 的回复切成指令块。**不认识的信息串必须报出来**，不静默忽略。"""
    blocks: List[Block] = []
    diags: List[Diagnostic] = []
    lines = (text or "").splitlines()
    index = 0
    while index < len(lines):
        match = FENCE.match(lines[index])
        if not match:
            index += 1
            continue
        info = match.group(1).strip()
        index += 1
        body: List[str] = []
        closed = False
        while index < len(lines):
            if lines[index].strip().startswith("```"):
                closed = True
                index += 1
                break
            body.append(lines[index])
            index += 1
        if not closed:
            diags.append(Diagnostic("LLM_BLOCK_UNCLOSED", WARNING,
                                    "有一个代码块没有闭合，其内容按原样取到最后一行"))
        text_body = "\n".join(body).strip("\n")
        kind, _, arg = info.partition(" ")
        kind = kind.strip().lower()
        if kind in ("", "puppet"):
            blocks.append(Block("batch", text_body))
        elif kind in ("puppet-replace", "replace"):
            blocks.append(Block("replace", text_body, info=info))
        elif kind in ("remember", "forget"):
            blocks.append(Block(kind, text_body, info=info))
        elif kind == "fuse":
            blocks.append(Block("fuse", text_body, info=info))
        elif kind == "write":
            path = arg.strip()
            if not path:
                diags.append(Diagnostic("LLM_BLOCK_UNKNOWN", WARNING,
                                        "write 块没有给出目标路径，已忽略"))
                continue
            blocks.append(Block("write", text_body, path=path, info=info))
        elif kind == "ask":
            blocks.append(Block("ask", text_body, info=info))
        elif kind == "tell":
            # `​```tell <app> <topic>```：给协作网络里的 peer 发消息（V4 A）。
            # 第一参数是 app 名（不是 #地址——收件人是一个**实例**，不是节点）。
            parts = arg.split()
            if len(parts) < 2 or not parts[1]:
                diags.append(Diagnostic("LLM_BLOCK_UNKNOWN", WARNING,
                                        "tell 块需要 `<app名> <topic>` 两个参数，已忽略"))
                continue
            blocks.append(Block("tell", text_body, path=parts[0], info=parts[1]))
        elif kind == "goal":
            # `​```goal```：设定自主回路的目标（V4 C）。body 即目标文本。
            blocks.append(Block("goal", text_body, info=info))
        else:
            diags.append(Diagnostic("LLM_BLOCK_UNKNOWN", WARNING,
                                    "不认识的代码块 `%s`，已忽略（有效的是 puppet / "
                                    "puppet-replace / write <路径> / ask / remember / forget / "
                                    "tell <app> <topic> / goal）" % info))
    return blocks, diags


def explanation_of(text: str) -> str:
    """块外的文字 = 给人看的说明。"""
    kept = []
    inside = False
    for line in (text or "").splitlines():
        if FENCE.match(line):
            inside = not inside
            continue
        if not inside:
            kept.append(line)
    return "\n".join(kept).strip()


def extract_calls(lines: List[str]) -> List[str]:
    """从命令批里挖出所有 `call <能力>` 的名字（含处理器动作里的）。

    读**执行链**而不是"看起来像"：批量里的 `call` 可能藏在 `on …:` 的动作体里。
    """
    stmts, _ = parse_program(list(lines))
    found: List[str] = []

    def take(actions) -> None:
        for action in actions or []:
            if getattr(action, "kind", "") == "call" and getattr(action, "func", ""):
                found.append(action.func)

    for stmt in stmts:
        if isinstance(stmt, CallStmt):
            found.append(stmt.func)
        elif isinstance(stmt, ActionStmt) and stmt.action is not None:
            take([stmt.action])
        elif isinstance(stmt, On):
            take(stmt.actions)
    return found


def risk_of(name: str) -> str:
    lowered = str(name).lower()
    for word in DANGER_WORDS:
        if word in lowered:
            return "能力名命中危险关键字 %r" % word
    return ""


# ------------------------------------------------------------------ 结果

@dataclass
class TurnResult:
    request: str = ""
    text: str = ""
    explanation: str = ""
    applied: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    diagnostics: List[dict] = field(default_factory=list)
    question: Optional[dict] = None
    pending: Optional[dict] = None
    stuck: str = ""
    halted: bool = False
    error: str = ""
    prompt_chars: int = 0

    def to_dict(self) -> dict:
        return {"request": self.request, "explanation": self.explanation,
                "applied": self.applied, "skipped": self.skipped,
                "diagnostics": self.diagnostics, "question": self.question,
                "pending": self.pending, "stuck": self.stuck, "halted": self.halted,
                "error": self.error, "prompt_chars": self.prompt_chars}


# ------------------------------------------------------------------ 回路

class Chat:
    def __init__(self, session, *, provider, prompts, storage, memory=None, log,
                 on_delta: Optional[Callable[[str], None]] = None,
                 confirm: Optional[Callable[[dict], bool]] = None,
                 origin: str = "llm", fail_budget: int = FAIL_BUDGET,
                 autonomous_allow=(), max_batch_lines: int = 0):
        self.session = session
        self.provider = provider
        self.prompts = prompts                 # [(名字, 实例)]
        self.storage = storage
        self.memory = memory                   # 运行期记忆（状态，不是程序）
        self.log = log
        self.on_delta = on_delta
        self.confirm = confirm
        # **写者身份**决定落盘文件与 origin 标记：共作者写 chat.jsonl，自主写
        # autonomous.jsonl——两本过程账分开，"它当时为什么这么做"才查得回来。
        self.origin = origin
        self.fail_budget = fail_budget         # 自主模式没人盯着，预算必须更紧
        self.autonomous_allow = frozenset(autonomous_allow)   # 自主白名单（只能人预先给）
        self.max_batch_lines = max_batch_lines  # 0 = 不限（自主模式由 runner 设定）
        self.readonly = False                  # 写者不是自己时置位：只读提问
        self.streaming = False                 # 流式中（插件热重载的护栏读它）
        self.mode = "执行"
        self.halted = False
        self.stuck_note = ""
        self.last_prompt: Optional[dict] = None      # 基础上下文块（可查看）
        self.last_messages: List[dict] = []           # 本轮**实际发送**的消息（可查看）
        self.last_chain: List[Tuple[str, bool]] = []
        self.pending: Optional[dict] = None
        self._prev_keys: set = set()
        self._fail_streak = 0
        self._approved: set = set(self.storage.read_json(".puppethub/approvals.json", []) or [])
        self._asset_manifest: set = set(self.storage.read_json(".puppethub/assets.json", []) or [])

    @property
    def history_rel(self) -> str:
        return ".puppethub/autonomous.jsonl" if self.origin == "autonomous" \
            else ".puppethub/chat.jsonl"

    # ------------------------------------------------------------ 历史（过程记忆）

    def history(self) -> List[dict]:
        return self.storage.read_jsonl(self.history_rel)

    def _append_history(self, role: str, text: str, **extra) -> None:
        entry = {"role": role, "text": text, "time": time.strftime("%Y-%m-%d %H:%M:%S")}
        entry.update(extra)
        try:
            self.storage.append_jsonl(self.history_rel, entry)
        except Exception as ex:  # noqa: BLE001 - 已进脏标记，不重复报
            self.log("error", "ERROR", "对话历史写入失败：%s" % ex)
            return
        self._trim_history()

    def _trim_history(self) -> None:
        """按轮数丢最旧，**上限与注入的 K 轮同值**——文件里有的就是上下文里有的。

        "不做摘要"与"设上限丢弃"不矛盾：摘要有损且会让幻觉被当真；
        **丢弃原文只是真丢、不产生假内容**。但丢弃是数据损失，所以必须可见。
        """
        entries = self.history()
        if len(entries) <= HISTORY_K:
            return
        kept = entries[-HISTORY_K:]
        try:
            self.storage.write_text(self.history_rel,
                                    "".join(_json_line(item) for item in kept))
        except Exception as ex:  # noqa: BLE001
            self.log("error", "ERROR", "对话历史裁剪失败：%s" % ex)
            return
        self.log("info", "HISTORY_TRUNCATED",
                 "对话历史超过 %d 轮，已丢弃最旧的 %d 轮（原文丢弃，不做摘要）"
                 % (HISTORY_K, len(entries) - len(kept)))

    # ------------------------------------------------------------ 一轮

    def turn(self, request: str) -> TurnResult:
        result = TurnResult(request=request)
        # **写者状态机**：只有当前写者能行动。共作者在自主回路当值时退化为只读提问；
        # 自主回路在写者不是它时（暂停 / 共作者当值）干脆不步进。
        writer = getattr(self.session, "writer", self.origin)
        self.readonly = writer != self.origin
        if self.origin == "autonomous" and self.readonly:
            result.error = "自主回路未当值（当前写者：%s）" % writer
            return result
        if self.halted:
            result.halted = True
            result.error = ("已停止自动重试（连续失败超过 %d 次）。请人工接管，"
                            "或明确要求\"推倒重来\"。" % self.fail_budget)
            self.log("error", "LLM_HALTED", result.error)
            return result
        self._append_history("user", request)

        context = self._context(request)
        self.last_prompt = context
        result.prompt_chars = context["size"]["total"]
        try:
            self.streaming = True
            try:
                text = self._stream(self._messages(context))
            finally:
                self.streaming = False
        except Exception as ex:  # noqa: BLE001 - provider 失败：保留已流出片段（见 §16.7）
            result.error = "%s" % ex
            self._note_failure()
            self.log("error", "LLM_PROVIDER", "本轮失败（已流出的片段保留）：%s" % ex)
            self._append_history("assistant", "", error=result.error)
            return result

        result.text = text
        result.explanation = explanation_of(text)
        self._append_history("assistant", text)
        blocks, diags = parse_response(text)
        result.diagnostics = [d.to_dict() for d in diags]
        self._absorb(diags)

        if not blocks:
            # **没有指令块 = 本轮什么都不改**。这必须说出来，否则人以为改了。
            self.log("info", "LLM_NO_ACTION", "本轮回复里没有任何指令块，程序未发生变化")
        self._dispatch(blocks, result)
        # 本轮注入过的记忆，回合末统一记一次"被访问"：每轮注入都写盘会让文件一直抖。
        if self.memory is not None:
            self.memory.flush_access()
        self._assess(result)
        return result

    # ------------------------------------------------------------ 上下文与提示

    def _context(self, request: str) -> dict:
        from . import context as _context
        diagnostics = [entry for entry in self.session.recent_diagnostics(40)]
        history = self.history()
        turns = [{"role": item.get("role", "user"), "text": item.get("text", "")}
                 for item in history[-HISTORY_K:]]
        limit = None
        if self.provider is not None and hasattr(self.provider, "context_limit"):
            try:
                limit = self.provider.context_limit()
            except Exception:  # noqa: BLE001
                limit = None
        return _context.build(
            app_dir=self.session.app, engine=self.session.engine,
            catalog=self.session.catalog(), diagnostics=diagnostics, turns=turns,
            rendering=self.session.hello()["rendering"], request=request,
            mode=self.mode, budget=limit, stuck=self.stuck_note,
            memory=self.memory.context_block() if self.memory is not None else "",
            fusion_brief=self.session.fusion_brief_text())

    def _messages(self, context: dict) -> List[dict]:
        """跑 prompt 插件链。每个插件可以**完全改写**上一步的结果。"""
        current = {"system": "", "messages": []}
        self.last_chain = []
        layers = [dict(context, system="", messages=[])]
        for name, plugin in self.prompts:
            try:
                produced = plugin.rewrite(layers[-1])
            except Exception as ex:  # noqa: BLE001 - prompt 失败回退 default（§16.7）
                self.log("error", "PLUGIN_INIT", "prompt 插件 %s 失败，已回退：%s" % (name, ex))
                self.last_chain.append((name, False))
                continue
            if not isinstance(produced, dict) or "messages" not in produced:
                self.log("error", "PLUGIN_INIT",
                         "prompt 插件 %s 返回的不是 {system, messages}" % name)
                self.last_chain.append((name, False))
                continue
            # "改写了" = 这个插件改动了它**拿到的** system / messages。
            # 不能拿 `produced` 与整个 `layers[-1]` 比：后者带全部上下文键
            # （source/turns/catalog…），而插件只回 {system, messages} ——
            # 两边不同构，比较结果恒为"改写了"，这句提示就永远在撒谎。
            before = layers[-1]
            changed = (produced.get("system", "") != before.get("system", "")
                       or list(produced.get("messages") or [])
                       != list(before.get("messages") or []))
            self.last_chain.append((name, changed))
            layers.append(dict(context, system=produced.get("system", ""),
                               messages=produced.get("messages", [])))
            current = produced
        if self.last_chain:
            self.log("info", "PROMPT_CHAIN",
                     "本轮上下文经 %s 处理" % "、".join(
                         "%s%s" % (name, "（改写了）" if changed else "")
                         for name, changed in self.last_chain))
        messages = []
        if current.get("system"):
            messages.append({"role": "system", "content": current["system"]})
        messages.extend(current.get("messages") or [])
        if not messages:
            # 一个 prompt 插件都没产出 → **不能发空请求**（那等于让 LLM 凭空猜），
            # 也不能装作正常：退回"只发原始上下文"，并把它说出来。
            messages = self._fallback_messages(context)
            self.log("error", "PLUGIN_PROMPT_FALLBACK",
                     "没有任何可用的 prompt 插件，已退回「只发原始上下文」的最小装配")
        # **本轮实际发送的完整 prompt 可查看**：不允许查看，prompt 就成了系统里唯一的黑盒。
        self.last_messages = messages
        return messages

    @staticmethod
    def _fallback_messages(context: dict) -> List[dict]:
        """最小装配：不做任何指令设计，只把原始上下文送过去（设计上核心里没有 prompt 逻辑）。"""
        import json
        payload = {key: context.get(key) for key in
                   ("mode", "source", "design", "catalog", "diagnostics", "assets")}
        return [{"role": "user", "content":
                 "【prompt 插件不可用，以下是原始上下文】\n%s\n\n【本轮请求】\n%s"
                 % (json.dumps(payload, ensure_ascii=False, default=str),
                    context.get("request") or "")}]

    def _stream(self, messages: List[dict]) -> str:
        if self.provider is None:
            raise RuntimeError("没有可用的 llm_provider：在 puppethub.toml 里配置，"
                               "并设置凭据环境变量")
        pieces: List[str] = []
        for piece in self.provider.stream(messages):
            pieces.append(piece)
            if self.on_delta is not None:
                self.on_delta(piece)
        return "".join(pieces)

    # ------------------------------------------------------------ 执行

    def _dispatch(self, blocks: List[Block], result: TurnResult) -> None:
        question = [block for block in blocks if block.kind == "ask"]
        if question:
            result.question = self._read_question(question[0])
            # 反问期**不执行任何写入**：分叉大时才问，问了就不要偷着改。
            # 自主模式的反问 = 挂起等人（runner 会按超时放弃），同样不写入。
            result.skipped = ["反问中：本轮不执行任何写入"]
            self.log("info", "LLM_ASK", "LLM 请求确认：%s"
                     % (result.question or {}).get("question", ""))
            return

        if self.readonly:
            # 写者不是自己（自主回路当值时人来提问，或反之）：**可见地**拒绝写入，
            # 而不是悄悄放行——两个写者并发改真源就是"单写者"机制的死亡。
            result.skipped = ["当前写者是 %s，本轮只读（%d 个指令块均未执行）"
                              % (getattr(self.session, "writer", "?"), len(blocks))]
            self.log("warning", "WRITER_READONLY", result.skipped[0])
            return

        if self.mode == "讨论":
            result.skipped = ["当前是**讨论**模式：本轮 %d 个指令块都没有执行"
                              % len(blocks)]
            self.log("info", "MODE_DISCUSS", result.skipped[0])
            return

        replace = [block for block in blocks if block.kind == "replace"]
        writes = [block for block in blocks if block.kind == "write"]
        batches = [block for block in blocks if block.kind == "batch"]
        remembers = [block for block in blocks if block.kind == "remember"]
        forgets = [block for block in blocks if block.kind == "forget"]

        if replace:
            for block in writes + batches + remembers + forgets:
                result.skipped.append("已选择整体替换，%s 块被跳过" % block.kind)
            self._request_pending("replace", replace[0].body, result,
                                  reason="整体替换不可回滚（人并不想回到上一版）")
            return

        fuses = [block for block in blocks if block.kind == "fuse"]
        if fuses:
            # 融合是**推倒重来级**的确认式动作，独占一轮：其余指令块一律跳过——
            # "合并 B"与"顺手改 A"混在同一轮里，确认卡就没法说清"将做什么"。
            for block in writes + batches + remembers + forgets:
                result.skipped.append("已选择融合，%s 块被跳过" % block.kind)
            for block in [b for b in blocks if b.kind in ("tell", "goal")]:
                result.skipped.append("已选择融合，%s 块被跳过" % block.kind)
            self._apply_fuse(fuses[0], result)
            return

        for block in writes:
            self._apply_write(block, result)

        call_names: List[str] = []
        for block in batches:
            call_names += extract_calls(block.body.splitlines())
        risky = self._needs_confirmation(call_names)
        if risky:
            self._request_pending("batch", "\n".join(b.body for b in batches), result,
                                  reason=risky, calls=sorted(set(call_names)))
            return

        for block in batches:
            self._apply_batch(block.body, result)
        # 记忆放最后：它记的常常是"这一轮干了什么、结果如何"。
        for block in remembers:
            self._apply_memory("remember", block.body, result)
        for block in forgets:
            self._apply_memory("forget", block.body, result)
        # 协作与目标（V4）：tell 不写真源但仍属"当值写者的动作"——readonly 分支
        # 已把非当值者挡在前面；goal 是方向盘，谁当值谁定方向，同样留痕。
        for block in [b for b in blocks if b.kind == "tell"]:
            peer = block.path
            topic = block.info
            if self.session.send_peer_message(peer, topic, block.body.strip()):
                result.applied.append("消息已发给 %s（topic=%s）" % (peer, topic))
            else:
                result.skipped.append("消息未送达 %s（总线不可用或投递失败）" % peer)
        for block in [b for b in blocks if b.kind == "goal"]:
            if self.session.set_goal(block.body.strip(), origin=self.origin):
                result.applied.append("目标已设定：%s" % block.body.strip()[:60])
            else:
                result.skipped.append("目标写入失败")

    # ------------------------------------------------------------ 运行期记忆

    def _apply_memory(self, kind: str, body: str, result: TurnResult) -> None:
        """记忆是**状态不是程序**，所以它不走命令批；但它仍然每写一条都留诊断。"""
        if self.memory is None:
            result.skipped.append("记忆不可用（storage 未装配）")
            return
        lines = [line.strip() for line in body.splitlines() if line.strip()]
        if not lines:
            result.skipped.append("%s 块是空的" % kind)
            return
        if kind == "remember":
            for line in lines:
                payload = parse_memory_entry(line)
                entry = self.memory.remember(payload["text"], importance=payload["importance"],
                                             tags=payload["tags"], origin=self.origin)
                if entry is not None:
                    result.applied.append("记住 %s（重要度 %d）：%s"
                                          % (entry["id"], entry["importance"],
                                             entry["text"][:30]))
        else:
            for line in lines:
                removed = self.memory.forget(line, origin=self.origin)
                if removed:
                    result.applied.append("忘掉 %d 条（%s）" % (removed, line[:30]))

    def _apply_batch(self, body: str, result: TurnResult) -> None:
        lines = [line for line in body.splitlines() if line.strip()]
        if not lines:
            return
        # **行动预算**（自主模式）：批行数上限。超出是**可见拒绝**，不是静默截断——
        # 截断会让"半批"被当成"整批"执行，那正是要防的。
        if self.max_batch_lines and len(lines) > self.max_batch_lines:
            result.skipped.append("命令批 %d 行超过自主预算 %d 行，已拒绝（拆小再试）"
                                  % (len(lines), self.max_batch_lines))
            self.log("error", "AUTONOMOUS_BUDGET", result.skipped[-1])
            return
        diags = self.session.send(lines, origin=self.origin)
        # 写者状态机在**写入路径**上核对：在途的那一轮（写者已被切走）会拿到
        # WRITER_DENIED。那种情况下命令批**没有**被应用，不能报成"命令批 N 行"
        # ——把被拒说成已应用，是"杜绝静默失败"要防的那种谎报。
        codes = {getattr(d, "code", None) or (d.get("code", "") if isinstance(d, dict) else "")
                 for d in (diags or [])}
        if "WRITER_DENIED" in codes:
            result.skipped.append("命令批 %d 行被拒（当前写者不是 %s）"
                                  % (len(lines), self.origin))
        else:
            result.applied.append("命令批 %d 行" % len(lines))
        for diag in diags or []:
            result.diagnostics.append(diag.to_dict() if hasattr(diag, "to_dict") else diag)
        self._absorb(diags)

    def _apply_write(self, block: Block, result: TurnResult) -> None:
        path = block.path.replace("\\", "/").lstrip("/")
        try:
            self._guard_write_path(path)
        except BlockError as ex:
            result.skipped.append("拒绝写入 %s：%s" % (path, ex))
            self.log("error", "LLM_WRITE_DENIED", "拒绝写入 %s：%s" % (path, ex))
            return
        self.session.app.push_snapshot("auto", "受限文件写入前自动存档", self.origin)
        target = self.session.app.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(block.body.rstrip("\n") + "\n")
        if path.startswith("assets/"):
            self._remember_asset(path)
        result.applied.append("写入 %s（%d 行，写前已自动存档）" % (path, len(block.body.splitlines())))
        self.log("info", "LLM_WRITE", "%s 写入文件 %s（origin=%s，写前已自动存档）"
                 % (self.origin, path, self.origin))
        if path.endswith("capabilities.py"):
            # 能力变了要**显式重载**：否则 LLM 以为加了能力，其实引擎里没有。
            self.session.reload()
            result.applied.append("已重载能力模块")

    def _guard_write_path(self, path: str) -> None:
        allowed = path == "capabilities.py" or path == "DESIGN.md" or path.startswith("assets/")
        if not allowed:
            raise BlockError("只允许写 capabilities.py / DESIGN.md / assets/**；"
                             "程序本身请用命令批")
        if "../" in path or path.startswith("/") or ":" in path:
            raise BlockError("路径必须是 app 目录内的相对路径")
        if path.startswith("assets/"):
            name = path[len("assets/"):]
            exists = (self.session.app.assets_dir / name).is_file()
            if exists and name not in self._asset_manifest:
                raise BlockError("这是**人放的资源**，LLM 不得覆盖它（改了文件名再写，"
                                 "或让人先删掉）")

    def _remember_asset(self, path: str) -> None:
        name = path[len("assets/"):]
        self._asset_manifest.add(name)
        self.storage.write_json(".puppethub/assets.json", sorted(self._asset_manifest))

    # ------------------------------------------------------------ 确认（危险动作）

    def _needs_confirmation(self, names: List[str]) -> str:
        """**两层**：能力名关键字命中即确认（醒目层）；任何能力**首次**调用确认一次（兜底层）。

        靠命名猜一定会漏——LLM 最爱发明新名字，`purge_cache()` 会直接通过关键字检查。
        兜底层把漏判的那一次拦住，成本是 O(能力数) 而不是 O(对话轮数)。
        """
        for name in names:
            if self.origin == "autonomous":
                # **自主模式没有人在场确认**：危险能力与首次调用一律**默认拒绝**，
                # 除非人**预先**把它列入自主白名单（白名单变更本身必须人来做）。
                if name not in self.autonomous_allow:
                    return ("能力 %s 未列入自主白名单，自主模式默认拒绝"
                            "（要允许就在 puppethub.toml 的 [autonomous] allow_calls 里预先声明）"
                            % name)
                continue
            if name not in self._approved:
                return "能力 %s 是**首次**调用，需要确认一次" % name
        for name in names:
            risk = risk_of(name)
            if not risk:
                continue
            if self.origin == "autonomous":
                if name not in self.autonomous_allow:
                    return "能力 %s 命中危险关键字，且未列入自主白名单，默认拒绝" % name
                continue               # 人预先授权过：白名单说了算
            return risk
        return ""

    def _request_pending(self, kind: str, body, result: TurnResult,
                         reason: str, calls=None) -> None:
        if self.origin == "autonomous":
            # 自主模式**没有人可确认**：挂起等人 = 卡死。所以直接拒绝并可见报警，
            # 让回路在下轮换方向（"危险动作要人做"本身就是给它的一条诊断）。
            result.skipped.append("自主模式拒绝需要人确认的动作：%s" % reason)
            self.log("error", "AUTONOMOUS_DENIED",
                     "自主回路请求 %s（%s）被默认拒绝。危险动作只能人预授权或人执行"
                     % (kind, reason))
            return
        self.pending = {"kind": kind, "body": body, "reason": reason,
                        "calls": calls or []}
        preview = body if isinstance(body, str) else \
            json.dumps(body, ensure_ascii=False, indent=2)
        result.pending = {"kind": kind, "reason": reason, "calls": calls or [],
                          "preview": preview[:2000]}
        result.skipped.append("**需要你确认**：%s。未执行任何写入。" % reason)
        self.log("warning", "CONFIRM_REQUIRED",
                 "待确认（%s）：%s。确认前不会写入" % (kind, reason))

    def approve_pending(self) -> None:
        """人确认后执行挂起的那一批。"""
        item, self.pending = self.pending, None
        if not item:
            return
        result = TurnResult(request="（确认后执行）")
        for name in item.get("calls") or []:
            self._approved.add(name)
        self.storage.write_json(".puppethub/approvals.json", sorted(self._approved))
        if item["kind"] == "fuse":
            outcome = self.session.fuse(item["body"], origin=self.origin)
            if outcome["ok"]:
                result.applied.append("融合完成（%d 行，B 已归档）"
                                      % outcome.get("merged_lines", 0))
            else:
                for error in outcome.get("errors") or []:
                    self.log("error", "FUSION_FAILED", error)
                result.error = "融合未执行：%s" % (outcome.get("errors") or ["见观察流"])
        elif item["kind"] == "batch":
            self._apply_batch(item["body"], result)
        else:
            self._apply_replace(item["body"], result)
        self.log("info", "CONFIRM_APPLIED",
                 "已按确认执行（%s），本次结果：%s"
                 % (item["kind"], "、".join(result.applied) or "无变化"))
        for diag in result.diagnostics:
            self.log("info", diag.get("code", "?"), diag.get("message", ""))

    def reject_pending(self) -> None:
        if self.pending:
            self.log("info", "CONFIRM_REJECTED", "已拒绝待确认的改动，程序未变化")
        self.pending = None

    # ------------------------------------------------------------ 融合（V3）

    def _resolve_b(self, b_path: str):
        """B 路径解析：相对路径以 **A 的父目录**为基准（融合对象天然是同级 app，
        与 hub 的发现语义一致）；绝对路径照用。必须是合法 app。"""
        from pathlib import Path
        from .appdir import AppDir
        base = Path(self.session.app.root).parent
        target = Path(b_path)
        resolved = target if target.is_absolute() else (base / target)
        resolved = Path(os.path.normpath(str(resolved)))
        if not (resolved / "app.puppet").is_file():
            self.log("error", "FUSION_REJECTED",
                     "融合对象 %s 不是 app 目录（缺少 app.puppet）" % resolved)
            return None
        return AppDir(resolved)

    def _apply_fuse(self, block: Block, result: TurnResult) -> None:
        from .fusion import audit_plan, default_plan
        try:
            payload = json.loads(block.body or "{}")
        except json.JSONDecodeError as ex:
            result.skipped.append("fuse 块不是合法 JSON（%s），已忽略" % ex)
            self.log("error", "FUSION_REJECTED", result.skipped[-1])
            return
        if not isinstance(payload, dict):
            self.log("error", "FUSION_REJECTED", "fuse 块必须是 JSON 对象")
            return

        if payload.get("cancel"):
            self.session.fusion_target = None
            self.log("info", "FUSION_CANCEL", "融合意图已取消")
            result.applied.append("融合意图已取消")
            return

        b_path = str(payload.get("b") or "").strip()
        if not b_path:
            self.log("error", "FUSION_REJECTED",
                     "fuse 块缺少 b 路径（{\"b\": \"../beta\"}）")
            return
        b = self._resolve_b(b_path)
        if b is None:
            return

        # 方案字段（有 = 方案轮；没有 = 意向轮：只声明对象，摘要下一轮注入）
        fields = {key: payload[key] for key in
                  ("renames", "caps", "window_title", "intent") if key in payload}
        unknown = set(payload) - {"b"} - set(fields) - {"cancel"}
        if unknown:
            result.skipped.append("fuse 块里的未知字段被忽略：%s（可用：renames / caps / "
                                  "window_title / intent）" % "、".join(sorted(unknown)))
        resolved = str(b.root)
        if not fields or self.session.fusion_target != resolved:
            # 意向轮（或换了融合对象）：声明目标 + 摘要注入。方案字段此时没有依据
            # ——B 的摘要还没进过上下文，接了就是让 LLM 瞎出方案。
            if fields:
                result.skipped.append("融合对象刚声明/变更，方案字段本轮被忽略"
                                      "（下一轮基于摘要出方案）")
            self.session.fusion_target = resolved
            self.log("info", "FUSION_INTENT",
                     "融合对象已声明：%s——B 的结构摘要已进下一轮上下文，请基于它出方案"
                     % b.root)
            result.applied.append("融合对象已声明：%s" % b.root)
            return

        plan = default_plan(self.session.app, b)
        plan.update(fields)
        plan["b"] = str(b.root)          # 方案里固化**解析后**的绝对路径——下游不再各自猜基准

        report = audit_plan(self.session.app, b, plan)
        if not report["ok"]:
            # 体检拒绝 → **诊断回灌**（本项目的核心回路）：下一轮 LLM 拿着错误清单修方案
            for error in report["errors"]:
                self.log("error", "FUSION_REJECTED", error)
            result.diagnostics.append({"code": "FUSION_REJECTED", "level": "error",
                                       "message": "；".join(report["errors"])})
            result.skipped.append("融合方案未通过体检（未执行），错误已回灌")
            return
        self._request_pending(
            "fuse", plan, result,
            reason="融合 %s → %s（推倒重来级确认式变更；B 将归档改名，不删除）"
                   % (b.name, self.session.app.name))

    # ------------------------------------------------------------ 整体替换

    def _read_question(self, block: Block) -> dict:
        import json
        try:
            payload = json.loads(block.body or "{}")
        except json.JSONDecodeError as ex:
            self.log("warning", "LLM_ASK", "ask 块不是合法 JSON（%s），按纯文本处理" % ex)
            return {"question": block.body.strip(), "options": [], "default": ""}
        if not isinstance(payload, dict):
            return {"question": str(payload), "options": [], "default": ""}
        return {"question": str(payload.get("question", "")),
                "options": list(payload.get("options") or []),
                "default": str(payload.get("default", ""))}

    def _apply_replace(self, body: str, result: TurnResult) -> None:
        if self.origin == "autonomous" or self.readonly:
            # 推倒重来是**确认式**动作：自主模式没有人在场确认，一律拒绝——
            # 这不是能力问题，是"意图必须可归属"问题。
            result.skipped.append("整体替换需要人确认，自主模式（或只读期）拒绝")
            self.log("error", "AUTONOMOUS_DENIED", result.skipped[-1])
            return
        lines = [line for line in body.splitlines() if line.strip()]
        self.session.app.push_snapshot("rebuild", "推倒重来前的兜底存档", "llm")
        diags = self.session.load_source(lines, origin="llm")
        result.applied.append("整体替换程序（%d 行，替换前已存兜底快照，不参与淘汰）" % len(lines))
        for diag in diags or []:
            result.diagnostics.append(diag.to_dict() if hasattr(diag, "to_dict") else diag)
        self._absorb(diags)
        self.session.app.append_decision("整体替换程序", "推倒重来（人已确认）", "全程序")

    # ------------------------------------------------------------ 卡住检测

    def _assess(self, result: TurnResult) -> None:
        # 重复键 = 码 + **位置** + 消息（设计 5.2）。行号必须进键：
        # 只用 (码, 消息) 会把"同一错误在两轮里都出现"与"错误跟着改动挪了行"混在一起——
        # 前者是卡住，后者至少说明它在改程序。换行号之后不算重复；
        # "一直在失败"这件事由连续失败计数（`FAIL_BUDGET`）兜住，两个信号各管一半。
        keys = {(item.get("code", ""), int(item.get("line") or 0),
                 item.get("message", ""))
                for item in result.diagnostics}
        errors = [item for item in result.diagnostics if item.get("level") == "error"]
        repeated = bool(keys & self._prev_keys)
        self._prev_keys = keys

        if repeated:
            self.stuck_note = ("同一诊断（含位置）在最近两轮里重复出现——大概率是理解偏差，"
                               "换个写法或换个方向。")
            result.stuck = self.stuck_note
            self.log("warning", "LLM_STUCK", self.stuck_note)
        else:
            self.stuck_note = ""

        if errors:
            self._fail_streak += 1
        else:
            self._fail_streak = 0
        if self._fail_streak > self.fail_budget:
            self.halted = True
            result.halted = True
            self.log("error", "LLM_HALTED",
                     "连续 %d 轮有错误，已停止自动重试：请人工接管，或要求\"推倒重来\""
                     % self._fail_streak)

    def _note_failure(self) -> None:
        self._fail_streak += 1
        if self._fail_streak > self.fail_budget:
            self.halted = True

    def resume(self, origin: str = "user") -> None:
        """人工接管：解除"停止自动重试"，把计数清零。

        `halted` 是**可恢复**的状态，不是死刑——检测器不知道人看到了什么，而人可能
        正好知道问题出在哪（"那个能力根本没装"）。恢复本身必须留痕，否则
        "它怎么又能跑了"就成了悬案。
        """
        self.halted = False
        self._fail_streak = 0
        self._prev_keys = set()
        self.stuck_note = ""
        self.log("info", "LLM_RESUME",
                 "人工接管：已解除停止、清零失败与重复计数（origin=%s）" % origin)

    def _absorb(self, diags) -> None:
        for diag in diags or []:
            if isinstance(diag, Diagnostic):
                self.log("info" if diag.level == INFO else
                         ("warning" if diag.level == WARNING else "error"),
                         diag.code, diag.message)


def parse_memory_entry(line: str) -> dict:
    """一行 = 一个 JSON 对象；**不是 JSON 就当成纯文本**。

    对 LLM 宽容（它常常直接写一句话），但只有这两种含义，不猜第三种。
    """
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return {"text": line, "importance": 1, "tags": []}
    if not isinstance(payload, dict):
        return {"text": line, "importance": 1, "tags": []}
    try:
        importance = int(payload.get("importance", 1))
    except (TypeError, ValueError):
        importance = 1
    tags = payload.get("tags") or []
    if not isinstance(tags, list):
        tags = [str(tags)]
    return {"text": str(payload.get("text", "")).strip(),
            "importance": importance, "tags": tags}


def _json_line(payload: dict) -> str:
    import json
    return json.dumps(payload, ensure_ascii=False) + "\n"
