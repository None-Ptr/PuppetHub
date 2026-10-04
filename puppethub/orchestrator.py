"""调度官：**社会层的 agent**（首页右栏那台 LLM）。

五要素在这里对齐（`docs/design-home.md` §5）：

- 决策 = LLM（每轮输出一个 ```society 块，每行一个动词）；
- 观察 = 项目列表 + 运行态（TCP hello）+ 总线尾 + 各端 `hello`；
- 行动 = `SocietyOps` 白名单动词；
- 工具 = 同上（它没有别的工具，**也没有 `send`/`load`**）；
- 记忆 = `$PUPPETHUB_HOME/orchestrator/`（转录 / 审计 / 目标 / 长期记忆）。

**它与驾驶舱 LLM 是两个 agent**（各自会话、各自预算、各自审计）：调度官要常驻
（社会视野不随窗口切换丢失），驾驶舱只服务一个 app。把两者塞进一个会话会让
"我在跟谁说话"变成需要推理的事。

**不变量**：调度官**永不写任何 app 的真源**。它拿不到 `Session`，只看得到
`SocietyOps`——`send`/`load` 在那层根本不存在。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from . import recent
from .chat import FENCE, explanation_of
from .secrets import _read_table, home_dir

ORCH_CONFIG_FILE = "orchestrator.toml"
ORCH_DIR = "orchestrator"
HISTORY_K = 12                 # 与 chat.HISTORY_K 同值：文件里有的就是上下文里有的
BLOCK_INFO = "society"

DEFAULTS = {
    "enable": True,            # 完全自主（总线消息即触发源）
    "fail_budget": 2,          # 与 app 侧自主同口径：没人盯着，预算更紧
    "steps_per_hour": 60,
    "max_commands": 30,        # 单轮动词上限：与"每轮 ≤30 行"同一条纪律
    "debounce_seconds": 3.0,   # 唤醒合并：一条一调 LLM 会烧穿预算
    "repeat_window": 600,      # 防循环：同一 (目标, topic) 在 N 秒内不重复自主发送
    "reflect_every": 20,       # 每 N 步插一步反思（0=关）
    "allow": [],               # 自主白名单：**只能人手动给**（调度官改不了自己）
    "call_timeout": 30,
}

CONFIG_HEADER = (
    "# 首页调度官的配置（`docs/design-home.md` §5）。\n"
    "# 模型指向：与 app 里的 puppethub.toml 同字段同语义。\n"
    "#   [llm]\n"
    "#   profile = \"deepseek\"      # 留空 = providers.toml 里唯一那个自动生效；\n"
    "#                            # 有多个而不指定则**报错不猜**\n"
    "#   [plugins.openai-compat]   # 也可直接写端点（会覆盖 profile）\n"
    "#   model = \"…\"\n"
    "#   [autonomous]\n"
    "#   enable = true             # 总线消息即触发源\n"
    "#   allow = [\"up\", \"open\"]     # 自主时默认只允许只读与 tell；\n"
    "#                            # 其余动词必须**人手动**加进这里\n")


def config_path() -> Path:
    return home_dir() / ORCH_CONFIG_FILE


def data_dir() -> Path:
    return home_dir() / ORCH_DIR


def read_config() -> dict:
    """`orchestrator.toml` 的原始表（一层表 + `[plugins.x]` 内层表）。"""
    return _read_table(config_path())


def resolve_model(raw: Optional[dict] = None) -> dict:
    """解析"用哪个模型"。返回 `{options, profile, note, error}`。

    与 app 侧 `session._apply_llm_profile` **同一条语义**：app/orchestrator 里的显式
    键优先，profile 只补缺省。缺省规则（`design-home.md` §5.3）：唯一 profile 自动用、
    0 个 = 未配置、≥2 个 = **报错不猜**。
    """
    from . import secrets
    raw = read_config() if raw is None else raw
    options = dict((raw.get("plugins") or {}).get("openai-compat") or {})
    options = {k: v for k, v in options.items() if v not in (None, "")}
    name = str((raw.get("llm") or {}).get("profile") or "").strip()
    note = ""
    if not name and options.get("model"):
        # 直接写了 `[plugins.openai-compat] model`：不必再要 profile（与 app 侧同一条）
        return {"options": options, "profile": "", "note": "", "error": ""}
    if not name:
        table = secrets.profiles()
        if len(table) == 1:
            name = next(iter(table))
            note = ("没有指定 profile：%s 里只有一个（%s），自动用它"
                    % (secrets.providers_path(), name))
        elif not table:
            return {"options": options, "profile": "", "note": "",
                    "error": ("没有配置模型：%s 里没有 profile，%s 也没写\n"
                              "  加一个 profile：\n"
                              "    [deepseek]\n"
                              "    base_url = \"https://api.deepseek.com/v1\"\n"
                              "    model = \"deepseek-chat\"\n"
                              "    key_env = \"DEEPSEEK_API_KEY\"\n"
                              "  再执行：puppethub keys set DEEPSEEK_API_KEY"
                              % (secrets.providers_path(), config_path()))}
        else:
            return {"options": options, "profile": "", "note": "",
                    "error": ("%s 里有 %d 个 profile（%s），但没指定用哪个——"
                              "在 %s 写 [llm] profile = \"名字\"。**不猜**。"
                              % (secrets.providers_path(), len(table),
                                 "、".join(sorted(table)), config_path()))}
    table = secrets.profile(name)
    if table is None:
        return {"options": options, "profile": name, "note": note,
                "error": secrets.profile_error(name)}
    merged = {key: table[key] for key in ("base_url", "model", "key_env")
              if table.get(key)}
    merged.update(options)                 # 显式键优先（与 app 侧一致）
    return {"options": merged, "profile": name, "note": note, "error": ""}


def build_provider(options: dict, log=None) -> Tuple[object, str]:
    """按 `options` 造一个 llm_provider（走既有的插件槽位，不新造一套）。"""
    from . import plugins as _plugins
    config = {"llm_provider": "openai-compat",
              "plugins": {"openai-compat": dict(options or {})}}
    registry = _plugins.build_registry()

    def _log(name, level, message):
        if log is not None:
            log("error" if level == "error" else "info", "PLUGIN", "%s：%s" % (name, message))

    instances, _names, diags = _plugins.select_slots(registry, config, log=_log)
    provider = instances.get("llm_provider")
    errors = [d.message for d in diags if getattr(d, "level", "") == "error"]
    if provider is None:
        return None, "；".join(errors) or "llm_provider 不可用"
    return provider, ""


# ------------------------------------------------------------------ 结果

@dataclass
class TurnResult:
    request: str = ""
    text: str = ""
    explanation: str = ""
    actions: List[dict] = field(default_factory=list)
    error: str = ""
    halted: bool = False
    stuck: str = ""
    autonomous: bool = False
    prompt_chars: int = 0

    def summary(self) -> str:
        if self.error:
            return self.error
        if not self.actions:
            return "本轮没有动词（社会未发生变化）"
        return "；".join(item.get("note") or item.get("verb", "")
                         for item in self.actions)


# ------------------------------------------------------------------ 解析

def parse_commands(text: str) -> Tuple[List[Tuple[str, str]], List[dict]]:
    """把回复切成 `[(动词, 参数串)]`。**不认识的块必须报出来**，不静默忽略。"""
    commands: List[Tuple[str, str]] = []
    diags: List[dict] = []
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
            diags.append({"code": "ORCH_BLOCK_UNCLOSED", "level": "警告",
                          "message": "有一个代码块没有闭合，其内容按原样取到最后一行"})
        kind = info.split()[0].strip().lower() if info else ""
        if kind in ("", BLOCK_INFO):
            for line in body:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                verb, _, rest = line.partition(" ")
                commands.append((verb.strip().lower(), rest.strip()))
        else:
            diags.append({"code": "ORCH_BLOCK_UNKNOWN", "level": "警告",
                          "message": "不认识的代码块 `%s`，已忽略（这里只认 ```%s）"
                                     % (info, BLOCK_INFO)})
    return commands, diags


def split_args(rest: str) -> List[str]:
    """按空白切参数（`new` 用 `--at/--title` 带值开关，避免歧义）。"""
    return [part for part in str(rest or "").split() if part]


def flags_of(args: List[str]) -> Tuple[List[str], Dict[str, str]]:
    """把 `--key value…` 从位置参数里分出来（值吃到下一个 `--` 之前）。"""
    positional: List[str] = []
    flags: Dict[str, str] = {}
    current = None
    for token in args:
        if token.startswith("--"):
            current = token[2:]
            flags[current] = ""
            continue
        if current is None:
            positional.append(token)
        else:
            flags[current] = (flags[current] + " " + token).strip()
    return positional, flags


# ------------------------------------------------------------------ 调度官

class Orchestrator:
    def __init__(self, ops, *, log=None, on_delta=None, provider=None,
                 provider_error: str = "", raw_config: Optional[dict] = None):
        self.ops = ops
        self.log = log if log is not None else (lambda *_: None)
        self.on_delta = on_delta
        self.raw_config = read_config() if raw_config is None else raw_config
        self.config = dict(DEFAULTS)
        for section in ("autonomous", "society"):
            table = self.raw_config.get(section) or {}
            for key, value in table.items():
                self.config[key] = value
        self.provider = provider
        self.provider_error = provider_error
        self.enable = bool(self.config.get("enable", True)) and provider is not None
        self.goal = self._read_goal()
        self.halted = False
        self.stuck_note = ""
        self.last_messages: List[dict] = []
        self._prev_keys: set = set()
        self._fail_streak = 0
        self._steps: List[float] = []
        self._since_reflect = 0
        self._recent_tells: Dict[tuple, float] = {}
        # 自主队列：总线消息进来**只入队**（不阻塞投递线程），由一个工作线程
        # 按 debounce 窗口合并成一次唤醒。
        self._queue: List[dict] = []
        self._cv = threading.Condition()
        self._worker: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # ------------------------------------------------------------ 落点

    def _rel(self, name: str) -> Path:
        return data_dir() / name

    def _append_jsonl(self, path: Path, entry: dict) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as ex:
            self.log("error", "ORCH_AUDIT", "写 %s 失败：%s" % (path, ex))

    def history(self) -> List[dict]:
        path = self._rel("chat.jsonl")
        if not path.is_file():
            return []
        out = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        except (OSError, json.JSONDecodeError):
            self.log("warning", "ORCH_HISTORY", "转录 %s 有读不出的行，已跳过" % path)
        return out

    def audit(self, limit: int = 0) -> List[dict]:
        path = self._rel("actions.jsonl")
        if not path.is_file():
            return []
        out = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    out.append(json.loads(line))
        except (OSError, json.JSONDecodeError):
            return []
        return out[-limit:] if limit else out

    def _read_goal(self) -> str:
        path = self._rel("goal.json")
        if not path.is_file():
            return ""
        try:
            return str((json.loads(path.read_text(encoding="utf-8")) or {}).get("text") or "")
        except (OSError, json.JSONDecodeError):
            return ""

    def set_goal(self, text: str) -> bool:
        payload = {"text": (text or "").strip()[:400],
                   "time": time.strftime("%Y-%m-%d %H:%M:%S")}
        path = self._rel("goal.json")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        except OSError as ex:
            self.log("error", "ORCH_GOAL", "目标写入失败：%s" % ex)
            return False
        self.goal = payload["text"]
        return True

    # ------------------------------------------------------------ 观察

    def observe(self) -> dict:
        """**调度官能看到的全部事实**（缺什么就说缺什么，不编）。"""
        items = [recent.state_of(item["root"]) for item in recent.entries()]
        try:
            status = self.ops.status()
        except Exception as ex:  # noqa: BLE001 - 观察失败必须可见，不能装作"没有实例"
            status = {"rows": [], "bus_port": None,
                      "error": "运行态读不出来：%s" % ex}
        tail = []
        try:
            tail = self.ops.bus(limit=5).get("entries") or []
        except Exception as ex:  # noqa: BLE001
            tail = [{"error": "总线尾读不出来：%s" % ex}]
        return {"projects": [{"name": item["name"], "root": item["root"],
                              "mark": item["mark"]} for item in items],
                "status": status, "bus_tail": tail,
                "goal": self.goal, "allow": sorted(self.config.get("allow") or []),
                "autonomous": self.enable,
                "provider_error": self.provider_error}

    def _observation_text(self) -> str:
        view = self.observe()
        lines: List[str] = ["【项目列表】"]
        if not view["projects"]:
            lines.append("（空——我还没打开过任何 app；可以用 `new` 造一个）")
        for item in view["projects"]:
            lines.append("- %s  %s%s" % (item["name"], item["root"],
                                        ("  " + item["mark"]) if item["mark"] else ""))
        lines.append("【运行态】")
        if view["status"].get("error"):
            lines.append("（%s）" % view["status"]["error"])
        rows = view["status"].get("rows") or []
        if not rows:
            lines.append("（账本里没有实例：没有 up 过，也没有本页开的窗口）")
        for row in rows:
            state = ("在跑" if row.get("alive") else "无应答") if row.get("mode") == "remote" \
                else ("窗口在跑" if row.get("alive") else "窗口已退出")
            lines.append("- %s  %s  %s  pid=%s" % (
                row.get("name"), state,
                ("127.0.0.1:%s" % row.get("port")) if row.get("port") else "无端口",
                row.get("pid")))
        lines.append("【总线】端口 %s；最近："
                     % (view["status"].get("bus_port") or "未上线"))
        for entry in view["bus_tail"]:
            if entry.get("delivery"):
                lines.append("  · 投递回执 %s → ok=%s failed=%s"
                             % (entry.get("from"), entry["delivery"].get("ok"),
                                entry["delivery"].get("failed")))
            elif entry.get("error"):
                lines.append("  · %s" % entry["error"])
            else:
                lines.append("  · %s [%s] %s：%s"
                             % (entry.get("from"), entry.get("topic"),
                                entry.get("title") or "-",
                                (entry.get("text") or "")[:120]))
        lines.append("【社会层】自主：%s；自主白名单：%s；目标：%s"
                     % ("开" if self.enable else "关",
                        "、".join(view["allow"]) or "空",
                        view["goal"] or "（未设）"))
        return "\n".join(lines)

    def system_prompt(self) -> str:
        forbidden = "、".join(self.ops.FORBIDDEN)
        return (
            "你是 OpenPuppet 的**社会层调度官**：一台机器上多个 app（每个 app 自己是一个 "
            "agent）之间的调度者。\n"
            "\n"
            "你能做的事只有下面这些动词；**你改不了任何 app 的程序**——那不是权限问题，"
            "是你这里根本没有那个能力（`%s` 不存在）。想让某个 app 变化，就让它的"
            "当值写者去做：给它 `tell` 一条消息，或者 `call` 它**借出**的能力。\n"
            "\n"
            "输出格式：**一个 ```%s 代码块**，每行一个动词（其余文字是给人看的说明，"
            "短句、一句一行）。一次最多 %d 行。\n"
            "\n"
            "```%s\n"
            "ls                              # 项目列表\n"
            "status                          # 运行态（真实健康度）\n"
            "bus [条数]                      # 总线审计尾部\n"
            "who <app>                       # 对端的 hello：它声明了哪些交互/数据/借出哪些能力\n"
            "check                           # 只读探活：模型通不通\n"
            "open <app>                      # 开一个窗口（另起进程）\n"
            "up [<app>…]                     # 拉起无头实例（不带参数 = 列表里全部）\n"
            "down [<app>…]                   # 停止实例\n"
            "tell <app> <topic> <文本…>       # 经总线发消息（刺激，不写入；按 topic 路由）\n"
            "fire <app> <目标> <事件> [行] [值]  # 触发对方自己声明的 handler\n"
            "call <app> <能力> [json]          # 调用对方借出的能力（被调方可拒绝）\n"
            "new <名字> [--at <父目录>] [--title <标题…>]  # 生成最小骨架\n"
            "forget <app>                    # 从项目列表移除\n"
            "```\n"
            "\n"
            "纪律：\n"
            "1. **只依据【观察】里给出的事实**。缺什么就说缺什么，不要编造 app 的名字、"
            "端口、能力。\n"
            "2. 拒绝与失败要如实转述给人（对方没借出某个能力、实例连不上、不在账本里）"
            "——不要把它们说成成功。\n"
            "3. 不值得动手就说明理由、**一行动词都不输出**。\n"
            "4. 危险动作（拉起/停止/新建）在自主时会被默认拒绝，除非人预先把它写进 "
            "allow；被拒了别重试同一件事。" % (forbidden, BLOCK_INFO,
                                            int(self.config["max_commands"]), BLOCK_INFO))

    def build_messages(self, request: str, *, autonomous: bool = False) -> List[dict]:
        history = self.history()[-HISTORY_K:]
        messages = [{"role": "system", "content": self.system_prompt()}]
        messages.append({"role": "user", "content": self._observation_text()})
        for item in history:
            role = item.get("role")
            if role in ("user", "assistant") and item.get("text"):
                messages.append({"role": role, "content": item["text"]})
        head = "【自主触发】" if autonomous else "【人】"
        messages.append({"role": "user", "content": "%s%s" % (head, request)})
        self.last_messages = messages
        return messages

    # ------------------------------------------------------------ 一轮

    def turn(self, request: str, *, autonomous: bool = False) -> TurnResult:
        result = TurnResult(request=request, autonomous=autonomous)
        if autonomous and not self.enable:
            result.error = "自主回路未开启（模型不可用或已关闭）"
            return result
        if autonomous:
            reason = self._budget_reason()
            if reason:
                result.error = reason
                self._audit_turn(result)
                return result
        if self.halted:
            result.halted = True
            result.error = ("已停止自动重试（连续失败超过 %d 次）：请人工接管"
                            % self.config["fail_budget"])
            self.log("error", "ORCH_HALTED", result.error)
            return result
        if self.provider is None:
            result.error = self.provider_error or "没有可用的 llm_provider"
            self.log("error", "ORCH_PROVIDER", result.error)
            return result

        self._append("user", request)
        messages = self.build_messages(request, autonomous=autonomous)
        result.prompt_chars = sum(len(m.get("content") or "") for m in messages)
        try:
            pieces: List[str] = []
            for piece in self.provider.stream(messages):
                pieces.append(piece)
                if self.on_delta is not None:
                    self.on_delta(piece)
            text = "".join(pieces)
        except Exception as ex:  # noqa: BLE001 - provider 失败：已流出片段保留
            result.error = "%s" % ex
            self._append("assistant", "", error=result.error)
            self.log("error", "ORCH_PROVIDER", "本轮失败：%s" % ex)
            self._note_failure()
            self._audit_turn(result)
            return result

        result.text = text
        result.explanation = explanation_of(text)
        self._append("assistant", text)
        commands, diags = parse_commands(text)
        for diag in diags:
            self.log("warning", diag["code"], diag["message"])
        if not commands:
            self.log("info", "ORCH_NO_ACTION", "本轮回复里没有任何动词，社会未发生变化")
        result.actions = self._execute_all(commands, autonomous=autonomous)
        self._assess(result)
        self._audit_turn(result)
        return result

    # ------------------------------------------------------------ 执行

    def _execute_all(self, commands, *, autonomous: bool) -> List[dict]:
        out: List[dict] = []
        budget = int(self.config["max_commands"])
        if len(commands) > budget:
            # **可见拒绝**，不静默截断（截断会把"半批"当成"整批"执行）
            message = "动词 %d 行超过单轮预算 %d 行，已全部拒绝（拆小再试）" % (len(commands), budget)
            self.log("error", "ORCH_BUDGET", message)
            return [{"verb": "（整批）", "ok": False, "note": message}]
        for verb, rest in commands:
            out.append(self.execute(verb, rest, autonomous=autonomous))
        return out

    def execute(self, verb: str, rest: str, *, autonomous: bool = False) -> dict:
        ops = self.ops
        if verb in ops.FORBIDDEN or not callable(getattr(ops, verb, None)):
            # 兜底：解析器只认动词表，但白名单层也要拦一次——边界两处都写死
            message = "没有 `%s` 这个动词（调度官改不了 app 的程序）" % verb
            self.log("error", "ORCH_UNKNOWN_VERB", message)
            return {"verb": verb, "ok": False, "note": message}
        if autonomous and verb not in ops.FREE_WHEN_AUTONOMOUS \
                and verb not in (self.config.get("allow") or []):
            message = ("`%s` 未列入自主白名单（[autonomous] allow），自主时默认拒绝"
                       "——危险动作只能人手动授权" % verb)
            self.log("warning", "ORCH_DENIED", message)
            return {"verb": verb, "ok": False, "note": message}
        if autonomous and verb == "tell":
            blocked = self._throttled(rest)
            if blocked:
                return {"verb": verb, "ok": False, "note": blocked}
        try:
            outcome = self._call(verb, rest)
        except Exception as ex:  # noqa: BLE001 - 单条动词失败不炸整轮，但必须可见
            message = "%s: %s" % (type(ex).__name__, ex)
            self.log("error", "ORCH_ACTION", "`%s` 执行异常：%s" % (verb, message))
            return {"verb": verb, "ok": False, "note": "执行异常：%s" % message}
        outcome.setdefault("verb", verb)
        outcome.setdefault("ok", True)
        return outcome

    def _call(self, verb: str, rest: str) -> dict:
        args, flags = flags_of(split_args(rest))
        if verb == "ls":
            return self.ops.ls()
        if verb == "status":
            return self.ops.status()
        if verb == "bus":
            limit = int(args[0]) if args and args[0].isdigit() else 10
            return self.ops.bus(limit=limit)
        if verb == "check":
            return self.ops.check()
        if verb in ("who", "open", "forget"):
            if not args:
                return {"ok": False, "note": "`%s` 需要一个 app 名" % verb}
            return getattr(self.ops, verb)(args[0])
        if verb == "up":
            return self.ops.up(args or None)
        if verb == "down":
            return self.ops.down(args or None)
        if verb == "tell":
            if len(args) < 2:
                return {"ok": False, "note": "`tell` 需要 `<app> <topic> <文本…>`"}
            text = rest.split(None, 2)[2] if len(args) >= 3 else ""
            return self.ops.tell(args[0], args[1], text)
        if verb == "fire":
            if len(args) < 3:
                return {"ok": False, "note": "`fire` 需要 `<app> <目标> <事件> [行] [值]`"}
            row = args[3] if len(args) > 3 else None
            value = " ".join(args[4:]) if len(args) > 4 else None
            return self.ops.fire(args[0], args[1], args[2], row=row, value=value)
        if verb == "call":
            if len(args) < 2:
                return {"ok": False, "note": "`call` 需要 `<app> <能力> [json]`"}
            payload = None
            if len(args) > 2:
                raw = rest.split(None, 2)[2]
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError as ex:
                    return {"ok": False, "note": "参数不是合法 JSON（%s）：%s" % (ex, raw[:80])}
                if not isinstance(payload, dict):
                    return {"ok": False, "note": "参数必须是 JSON 对象"}
            return self.ops.call(args[0], args[1], payload)
        if verb == "new":
            if not args:
                return {"ok": False, "note": "`new` 需要一个目录名"}
            return self.ops.new(args[0], parent=flags.get("at", ""),
                               title=flags.get("title", ""))
        return {"ok": False, "note": "没有 `%s` 这个动词" % verb}

    # ------------------------------------------------------------ 节流 / 预算

    def _throttled(self, rest: str) -> str:
        """防循环：同一 `(目标, topic)` 在窗口内不重复**自主**发送（人不受限）。"""
        args = split_args(rest)
        if len(args) < 2:
            return ""
        key = (args[0], args[1])
        now = time.time()
        window = float(self.config.get("repeat_window") or 0)
        last = self._recent_tells.get(key)
        if window and last is not None and now - last < window:
            message = ("节流跳过：%d 秒内已经给 `%s` 发过 topic=`%s` 的消息"
                       % (int(window), key[0], key[1]))
            self.log("info", "ORCH_THROTTLED", message)
            return message
        self._recent_tells[key] = now
        return ""

    def _budget_reason(self) -> str:
        window = time.time() - 3600
        recent_steps = sum(1 for stamp in self._steps if stamp > window)
        limit = int(self.config["steps_per_hour"])
        if recent_steps >= limit:
            return ("自主步进已达每小时预算（%d 次/小时），挂起等待人调整——"
                    "不是静默丢弃" % limit)
        return ""

    # ------------------------------------------------------------ 自主

    def start_autonomous(self) -> None:
        if self._worker is not None or not self.enable:
            return
        self._worker = threading.Thread(target=self._loop, daemon=True,
                                        name="orchestrator")
        self._worker.start()

    def notify(self, entry: dict) -> None:
        """总线消息到达（社会层的观察者回调）。**只入队**，不在这里调 LLM。"""
        with self._cv:
            self._queue.append(entry)
            self._cv.notify_all()

    def _loop(self) -> None:
        while not self._stop.is_set():
            with self._cv:
                if not self._queue:
                    self._cv.wait(timeout=1.0)
                    continue
                deadline = time.time() + float(self.config.get("debounce_seconds") or 0)
                while self._queue and time.time() < deadline:
                    self._cv.wait(timeout=max(0.05, deadline - time.time()))
                batch, self._queue = self._queue[:], []
            trigger = "\n".join(
                "来自 %s 的消息（topic=%s）：%s"
                % (item.get("from"), item.get("topic"), (item.get("text") or "")[:200])
                for item in batch)
            result = self.turn(trigger, autonomous=True)
            self._steps.append(time.time())
            if result.halted or self.halted:
                self._circuit_break(trigger)
            else:
                self._maybe_reflect()

    def _circuit_break(self, trigger: str) -> None:
        self.log("error", "ORCH_CIRCUIT",
                 "调度官连续失败达到预算（%s 次），已熔断：自主停止，等人工接管"
                 % self.config["fail_budget"])
        self.enable = False

    def _maybe_reflect(self) -> None:
        every = int(self.config.get("reflect_every") or 0)
        if not every:
            return
        self._since_reflect += 1
        if self._since_reflect < every:
            return
        self._since_reflect = 0
        result = self.turn("【反思】回顾最近的调度动作，把值得长期记住的（谁的订阅是什么、"
                           "哪些 app 之间已经通了消息）用文字总结即可——不值得沉淀就"
                           "什么都不做。", autonomous=True)
        self._steps.append(time.time())
        if result.halted:
            self._circuit_break("反思")

    def stop(self) -> None:
        self._stop.set()
        with self._cv:
            self._cv.notify_all()

    # ------------------------------------------------------------ 记账

    def _append(self, role: str, text: str, **extra) -> None:
        entry = {"role": role, "text": text,
                 "time": time.strftime("%Y-%m-%d %H:%M:%S")}
        entry.update(extra)
        self._append_jsonl(self._rel("chat.jsonl"), entry)
        entries = self.history()
        if len(entries) > HISTORY_K:
            kept = entries[-HISTORY_K:]
            try:
                self._rel("chat.jsonl").write_text(
                    "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in kept),
                    encoding="utf-8")
                self.log("info", "ORCH_HISTORY_TRUNCATED",
                         "转录超过 %d 轮，已丢弃最旧的 %d 轮（原文丢弃，不做摘要）"
                         % (HISTORY_K, len(entries) - len(kept)))
            except OSError as ex:
                self.log("error", "ORCH_HISTORY", "转录裁剪失败：%s" % ex)

    def _audit_turn(self, result: TurnResult) -> None:
        self._append_jsonl(self._rel("actions.jsonl"), {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "autonomous": result.autonomous,
            "request": result.request[:400],
            "explanation": result.explanation[:400],
            "error": result.error,
            "halted": result.halted,
            "actions": [{"verb": item.get("verb"), "ok": item.get("ok"),
                         "note": (item.get("note") or "")[:300]}
                        for item in result.actions]})

    def _assess(self, result: TurnResult) -> None:
        errors = [item for item in result.actions if item.get("ok") is False]
        self._fail_streak = self._fail_streak + 1 if errors else 0
        keys = {(item.get("verb"), (item.get("note") or "")[:80]) for item in errors}
        if keys & self._prev_keys:
            self.stuck_note = "同一条拒绝在最近两轮里重复出现——换个做法，别硬试。"
            result.stuck = self.stuck_note
            self.log("warning", "ORCH_STUCK", self.stuck_note)
        else:
            self.stuck_note = ""
        self._prev_keys = keys
        if self._fail_streak > int(self.config["fail_budget"]):
            self.halted = True
            result.halted = True
            self.log("error", "ORCH_HALTED",
                     "连续 %d 轮被拒/失败，已停止自动重试：请人工接管"
                     % self._fail_streak)

    def _note_failure(self) -> None:
        self._fail_streak += 1
        if self._fail_streak > int(self.config["fail_budget"]):
            self.halted = True

    def resume(self) -> None:
        """人工接管：解除停止、清零计数与节流窗口（恢复本身留痕）。"""
        self.halted = False
        self._fail_streak = 0
        self._prev_keys = set()
        self.stuck_note = ""
        self._recent_tells.clear()
        self.log("info", "ORCH_RESUME", "人工接管：已解除停止、清零失败与节流")

    def stats(self) -> dict:
        window = time.time() - 3600
        return {"enable": self.enable, "allow": sorted(self.config.get("allow") or []),
                "steps_last_hour": sum(1 for s in self._steps if s > window),
                "budget_per_hour": int(self.config["steps_per_hour"]),
                "halted": self.halted, "goal": self.goal}
