"""首页冒烟：社会层 + 调度官 + MRU（**无窗口驱动**）。

要验证的六件事（`docs/design-home.md` §7）：

1. **边界**：`SocietyOps` 上**不存在** `send`/`load`——"手被物理切断"必须有测试，
   否则它会悄悄长回来；
2. **端到端社会**：临时 HOME + 两个真 app，`up` 之后 `tell` 真的送达对方进程
   （顺带证明"总线寿命"那个既有缺陷已修）；
3. **MRU**：成功打开才入册、全部历史无上限、失效条目不自动删、`forget` 生效；
4. **降级**：0 个 profile → 只读错误；1 个 → 自动用；≥2 个 → 报错不猜；
5. **自主护栏**：不在 allow 的动词被拒且**留审计**、节流跳过、`allow` 只能人给；
6. **首页纯逻辑**：运行态五种措辞（"未编排"= 不知道，不是"没在跑"）。

用法：python docs/smoke-home.py
"""

from __future__ import annotations

import inspect
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 子进程（真 app 实例）要能 `-m puppethub`：PYTHONPATH 必须带上仓库根。
_parts = [p for p in (os.environ.get("PYTHONPATH") or "").split(os.pathsep) if p]
if str(ROOT) not in _parts:
    os.environ["PYTHONPATH"] = os.pathsep.join([str(ROOT)] + _parts)


class FakeSociety:
    """给"一轮调度"用的假社会层：记账调用，不起进程。"""

    def __init__(self):
        self.calls = []

    def start(self):
        return {"port": 9999, "error": ""}

    def bus_port(self):
        return 9999

    def rows(self):
        return [{"name": "alpha", "root": None, "port": 9401, "pid": 111,
                 "mode": "remote", "alive": True}]

    def bus_tail(self, limit=10):
        return [{"time": "2026-10-04 12:00:00", "from": "alpha", "topic": "t",
                 "text": "hi"}]

    def tell(self, topic, text, title="", sender="society"):
        self.calls.append(("tell", topic, text))
        return {"ok": True, "delivered": ["beta"], "failed": []}

    def up(self, roots, on_event=None):
        self.calls.append(("up", tuple(roots)))
        return [{"name": "alpha", "ready": True}]

    def down(self, roots=None, on_event=None):
        self.calls.append(("down", roots))
        return {"stopped": [], "dead": []}

    def open_window(self, root, on_event=None):
        self.calls.append(("open", str(root)))
        return {"ok": True, "pid": 1}

    def request(self, token, payload, timeout=3.0):
        self.calls.append(("request", token, payload))
        return {"ok": True, "rendering": {}, "lend": ["shout"]}

    def known(self, token):
        return None


class FakeProvider:
    def __init__(self, script):
        self.script = script
        self.seen = []

    def stream(self, messages):
        self.seen.append(messages)
        for piece in self.script:
            yield piece

    def context_limit(self):
        return None


def main() -> int:
    home = Path(tempfile.mkdtemp(prefix="puppethub-homehome-"))
    os.environ["PUPPETHUB_HOME"] = str(home)
    failures: list = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print("  [%s] %s%s" % ("ok" if ok else "失败", label,
                               ("  ← " + str(detail)[:200]) if detail and not ok else ""))
        if not ok:
            failures.append(label)

    from puppethub import orchestrator, recent
    from puppethub.appdir import AppDir, create_app
    from puppethub.society import Society, SocietyOps

    # 端到端用：真程序 + 真能力（`fire` 要有自己声明的 handler、`call` 要有借出的能力）
    society_program = [
        'add #root window #win title="协作"',
        'add #win col #content pad=16',
        'add #content button #go text="走"',
        'on #go click:',
        '    set #win title="走了"',
    ]
    society_caps = ('"""能力。"""\nfrom puppet import capability\n\n\n'
                    '@capability(returns="str")\ndef shout(text: str) -> str:\n'
                    '    """原样返回。"""\n    return text\n')

    # ---------------------------------------------------------------- 1 边界
    print("· 白名单边界（手被物理切断）")
    for banned in SocietyOps.FORBIDDEN:
        check("SocietyOps 没有 %s" % banned, not hasattr(SocietyOps, banned))
    check("白名单动词都实现了",
          all(callable(getattr(SocietyOps, verb, None)) for verb in SocietyOps.VERBS),
          [v for v in SocietyOps.VERBS if not callable(getattr(SocietyOps, v, None))])
    check("调度官不给 Session 任何入口",
          not any("session" in name.lower() for name in dir(orchestrator.Orchestrator)))

    # ---------------------------------------------------------------- 2 MRU
    print("· MRU（打开过的 app）")
    work = Path(tempfile.mkdtemp(prefix="puppethub-homework-"))
    alpha = create_app(work, "alpha", "Alpha")
    beta = create_app(work, "beta", "Beta")
    ghost = work / "ghost"            # 存在但不是 app
    ghost.mkdir()
    missing = work / "nope"           # 不存在

    check("空历史", recent.entries() == [])
    recent.record(alpha.root)
    recent.record(alpha.root)         # 重复打开：只刷新时间，不新增
    check("重复打开不新增", len(recent.entries()) == 1, recent.entries())
    recent.record(beta.root)
    check("打开过的都在（倒序）",
          [item["root"] for item in recent.entries()] == [str(beta.root), str(alpha.root)],
          recent.entries())
    check("状态：是 app", recent.state_of(alpha.root)["mark"] == "")
    check("状态：不是 app", recent.state_of(ghost)["mark"] == "(不是 app)",
          recent.state_of(ghost))
    check("状态：已不在", recent.state_of(missing)["mark"] == "(已不在)",
          recent.state_of(missing))
    check("忘记一条", recent.forget(beta.root) and len(recent.entries()) == 1)
    check("忘记不存在的返回 False", recent.forget(beta.root) is False)
    for index in range(25):           # **无上限**：25 条全在
        recent.record(create_app(work, "many%02d" % index).root)
    check("全部历史无上限", len(recent.entries()) == 26, len(recent.entries()))

    # ---------------------------------------------------------------- 3 解析
    print("· 动词解析")
    commands, diags = orchestrator.parse_commands(
        "说明文字\n```society\nup alpha\n# 注释行\ntell alpha ping 你好 世界\n```\n")
    check("切出两条动词", [item[0] for item in commands] == ["up", "tell"], commands)
    check("tell 的正文带空格", commands[1][1] == "alpha ping 你好 世界", commands)
    _, diags2 = orchestrator.parse_commands("```puppet\nadd #x\n```")
    check("不认识的块要报出来", bool(diags2), diags2)
    args, flags = orchestrator.flags_of(
        orchestrator.split_args("new gamma --at D:/work --title 我的 应用"))
    check("new 的位置参数与带值开关", args == ["new", "gamma"]
          and flags == {"at": "D:/work", "title": "我的 应用"}, (args, flags))

    # ---------------------------------------------------------------- 4 模型解析
    print("· 模型来源（缺省规则）")
    check("0 个 profile → 未配置", bool(orchestrator.resolve_model()["error"]))
    from puppethub import secrets
    secrets._write_table(secrets.providers_path(),
                         {"deepseek": {"base_url": "https://api.deepseek.com/v1",
                                       "model": "deepseek-chat",
                                       "key_env": "DEEPSEEK_API_KEY"}})
    single = orchestrator.resolve_model()
    check("1 个 profile → 自动用它",
          not single["error"] and single["profile"] == "deepseek"
          and single["options"]["model"] == "deepseek-chat", single)
    secrets._write_table(secrets.providers_path(),
                         {"deepseek": {"model": "deepseek-chat"},
                          "other": {"model": "other-chat"}})
    many = orchestrator.resolve_model()
    check("≥2 个 profile → 报错不猜", "不猜" in many["error"], many)
    orchestrator.config_path().write_text(
        '[llm]\nprofile = "other"\n', encoding="utf-8")
    chosen = orchestrator.resolve_model()
    check("指定了就按指定的", chosen["profile"] == "other", chosen)
    orchestrator.config_path().write_text(
        '[plugins.openai-compat]\nmodel = "inline-model"\n', encoding="utf-8")
    inline = orchestrator.resolve_model()
    check("直接写 model → 不必再要 profile",
          not inline["error"] and inline["options"]["model"] == "inline-model", inline)
    orchestrator.config_path().unlink()

    # ---------------------------------------------------------------- 5 一轮调度
    print("· 调度官一轮（假 provider，真白名单）")
    fake = FakeSociety()
    ops = SocietyOps(fake, log=lambda *_: None,
                     config={"plugins": {"openai-compat": {}}})
    script = "我把 alpha 拉起来，并告诉它一声。\n```society\nup alpha\ntell alpha ping 你好\n```\n"
    orch = orchestrator.Orchestrator(
        ops, log=lambda *_: None, provider=FakeProvider([script]),
        raw_config={"autonomous": {"enable": True}})
    result = orch.turn("把 alpha 拉起来")
    check("两个动作都执行了", [item["verb"] for item in result.actions] == ["up", "tell"],
          result.actions)
    check("动作结果都成功", all(item["ok"] for item in result.actions), result.actions)
    check("说明文字被留下", "拉起来" in result.explanation, result.explanation)
    check("审计写了", len(orch.audit()) == 1, orch.audit())
    check("转录写了两行", len(orch.history()) == 2, orch.history())

    # 黑名单动词（解析器认动词表，白名单层也要拦）
    denied = orch.execute("send", "alpha", autonomous=True)
    check("`send` 被拒且说明没有这个动词", denied["ok"] is False
          and "send" in denied["note"], denied)

    # 自主护栏：默认拒绝 + 人给了才允许
    orch.provider = FakeProvider(["```society\nup alpha\n```"])
    autonomous = orch.turn("自己决定", autonomous=True)
    check("自主时 up 默认拒绝", autonomous.actions[0]["ok"] is False
          and "白名单" in autonomous.actions[0]["note"], autonomous.actions)
    check("被拒也留审计",
          orch.audit()[-1]["actions"][0]["ok"] is False, orch.audit()[-1])
    orch.config["allow"] = ["up"]
    allowed = orch.execute("up", "alpha", autonomous=True)
    check("人给 allow 之后放行", allowed["ok"] is True, allowed)

    # 节流：同一 (目标, topic) 自主时只发一次
    first = orch.execute("tell", "alpha ping 第一次", autonomous=True)
    second = orch.execute("tell", "alpha ping 第二次", autonomous=True)
    check("防循环节流", first["ok"] is True and second["ok"] is False
          and "节流" in second["note"], (first, second))
    human = orch.execute("tell", "alpha ping 人说的", autonomous=False)
    check("人不被节流", human["ok"] is True, human)

    # 降级：没有 provider
    offline = orchestrator.Orchestrator(ops, log=lambda *_: None, provider=None,
                                        provider_error="没有配置模型")
    offline_result = offline.turn("喂")
    check("无调度官时如实报错", bool(offline_result.error), offline_result)
    check("无调度官时不进自主", offline.enable is False)

    # ---------------------------------------------------------------- 5b 自主回路
    print("· 自主回路：总线消息 → debounce 合并成**一轮** → 审计")
    import time as _time
    auto_ops = SocietyOps(FakeSociety(), log=lambda *_: None,
                          config={"plugins": {"openai-compat": {}}})
    auto_provider = FakeProvider(["（暂时无事可做，本轮不改。）"])
    auto = orchestrator.Orchestrator(
        auto_ops, log=lambda *_: None, provider=auto_provider,
        raw_config={"autonomous": {"enable": True, "debounce_seconds": 0.1,
                                   "reflect_every": 0}})
    before = len(auto.audit())             # 审计文件是同一个：先取基线（否则读到前面几轮）
    auto.start_autonomous()
    for index in range(3):                 # 三条消息紧挨着来 → 该被合并，不是三次调用
        auto.notify({"from": "agent%d" % index, "topic": "t",
                     "text": "第 %d 条" % index, "time": "x"})
    deadline = _time.time() + 5.0
    while _time.time() < deadline and len(auto.audit()) <= before:
        _time.sleep(0.05)
    auto.stop()
    entries = auto.audit()[before:]
    check("自主回路真的跑了一轮", len(entries) == 1, entries)
    if entries:
        check("这一轮是自主轮", entries[0].get("autonomous") is True, entries[0])
        check("三条消息被 debounce 合并进同一轮",
              all(("第 %d 条" % index) in entries[0].get("request", "")
                  for index in range(3)), entries[0].get("request"))
    check("自主回路每轮都调一次 LLM（合并后 = 一次）",
          len(auto_provider.seen) == 1, len(auto_provider.seen))

    # ---------------------------------------------------------------- 5c 长期记忆
    print("· 调度官自己的长期记忆（`orchestrator/memory/`）")
    from puppethub.orchestrator import memory_store

    memory = memory_store(lambda *_: None)
    mem_ops = SocietyOps(FakeSociety(), log=lambda *_: None,
                         config={"plugins": {"openai-compat": {}}}, memory=memory)
    check("落点就是设计里那个 memory/", memory.rel == "memory/memory.jsonl", memory.rel)
    remember = mem_ops.remember("alpha 订阅 data，beta 订阅 ping", importance=2,
                                tags=["订阅"])
    check("记住一条", remember["ok"] and remember["id"] == "m1", remember)
    memory_file = Path(os.environ["PUPPETHUB_HOME"]) / "orchestrator" / "memory" \
        / "memory.jsonl"
    check("写到盘上（可读可改的纯文本）",
          memory_file.is_file()
          and "beta 订阅 ping" in memory_file.read_text(encoding="utf-8"), memory_file)
    check("注入块含这条", "beta 订阅 ping" in memory.context_block())
    memory.flush_access()
    check("回合末记一次访问（语义化裁剪要有依据）",
          memory.entries()[0]["uses"] == 1, memory.entries()[0])
    bare = SocietyOps(FakeSociety(), log=lambda *_: None)
    check("没装配记忆时可见拒绝",
          bare.remember("试试")["ok"] is False, bare.remember("试试"))

    mem_orch = orchestrator.Orchestrator(
        mem_ops, log=lambda *_: None,
        provider=FakeProvider(["```society\n"
                               "remember 人纠正过 up 的用法 --imp 3 --tags 教训\n```"]),
        raw_config={"autonomous": {"enable": True}})
    mem_result = mem_orch.turn("记一下")
    check("`remember` 动词可用（文本在前、开关在后）",
          mem_result.actions[0]["ok"] is True, mem_result.actions)
    second = [item for item in memory.entries() if item["id"] == "m2"]
    check("重要度与标签解析对了",
          bool(second) and second[0]["importance"] == 3
          and second[0]["tags"] == ["教训"], second)
    check("记忆块进了提示词",
          "beta 订阅 ping" in json.dumps(mem_orch.last_messages, ensure_ascii=False))
    check("自主时记忆默认允许（反思要能沉淀）",
          mem_orch.execute("remember", "自主时记一条", autonomous=True)["ok"] is True)
    check("列表 forget 仍默认拒绝（记忆放行不等于放松）",
          mem_orch.execute("forget", "alpha", autonomous=True)["ok"] is False)
    gone = mem_ops.unremember("alpha 订阅 data，beta 订阅 ping")
    check("按完整文本忘掉", gone["ok"] and gone["removed"] == 1, gone)
    miss = mem_ops.unremember("根本没有这条")
    check("忘不存在的条目如实报", miss["ok"] is False and "没有这一条" in miss["note"], miss)
    try:
        memory.storage.read_text("../../外面.txt")
        escaped = False
    except ValueError:
        escaped = True
    check("记忆适配器拒绝越界路径", escaped)

    # ---------------------------------------------------------------- 6 首页纯逻辑
    print("· 首页运行态措辞（「未编排」的意思是「我不知道」）")
    from puppethub.home import HomeWindow
    window = HomeWindow(fake, ops, None)
    window.rows = [{"mode": "remote", "root": str(alpha.root), "port": 9401,
                    "name": "alpha", "pid": 111, "alive": True, "mine": False},
                   {"mode": "window", "root": str(beta.root), "port": 9402,
                    "name": "beta", "pid": 222, "alive": True, "mine": True}]
    check("无头实例在跑",
          window._state_text(recent.state_of(alpha.root)) == "● 在跑（无头 :9401）",
          window._state_text(recent.state_of(alpha.root)))
    check("本页开的窗口",
          window._state_text(recent.state_of(beta.root)) == "● 在跑（本页开 :9402）",
          window._state_text(recent.state_of(beta.root)))
    other = create_app(work, "gamma").root
    check("看不见的实例不断言「没在跑」",
          window._state_text(recent.state_of(other)) == "○ 未编排")
    check("失效条目标记优先", window._state_text(recent.state_of(missing)) == "(已不在)")
    window.rows.append({"mode": "window", "root": str(alpha.root), "port": 9403,
                        "name": "alpha", "pid": 333, "alive": False, "mine": False})
    check("同名两条并存时并列显示",
          window._state_text(recent.state_of(alpha.root))
          == "● 在跑（无头 :9401） + ✕ 无应答（窗口）",
          window._state_text(recent.state_of(alpha.root)))

    # ---------------------------------------------------------------- 7 CLI 接线
    print("· CLI 接线（裸跑 = 首页；run 可注入 hub_port）")
    from puppethub import cli
    from puppethub import home as home_module
    from puppethub import window as window_module

    opened: dict = {}

    class FakeHome:
        def __init__(self, society, ops, orchestrator=None):
            opened["society"] = society
            opened["orchestrator"] = orchestrator

        def run(self):
            opened["ran"] = True

    real_home = home_module.HomeWindow
    home_module.HomeWindow = FakeHome
    try:
        code = cli.main(["--no-llm"])          # 裸跑（不带子命令）= 首页
    finally:
        home_module.HomeWindow = real_home
    check("裸跑走首页，--no-llm 时没有调度官",
          code == 0 and opened.get("ran") is True and opened.get("orchestrator") is None,
          opened)
    check("裸跑也认 --no-llm 这个全局开关", code == 0, code)

    code = cli.main(["run", str(work / "nope")])
    check("run 非 app 仍报错退出（语义不变）", code == 1, code)

    captured: dict = {}

    class FakeHubWindow:
        def __init__(self, session, listen_port=None):
            captured["session"] = session
            captured["listen_port"] = listen_port

        def run(self):
            captured["ran"] = True

    real_window = window_module.HubWindow
    window_module.HubWindow = FakeHubWindow
    try:
        code = cli.main(["run", str(alpha.root), "--hub-port", "9999",
                         "--listen-port", "9001"])
    finally:
        window_module.HubWindow = real_window
    check("run --hub-port 真把窗口实例接进总线",
          code == 0 and captured.get("ran") is True
          and captured["session"].bus is not None, captured.get("session"))
    check("run --listen-port 传进了窗口（窗口实例的耳朵）",
          captured.get("listen_port") == 9001, captured.get("listen_port"))
    from puppethub.society import window_command
    cmd = window_command(alpha.root, 9999, 9001)
    check("窗口命令两个端口都给（能说话 + 能被找到）",
          "--hub-port" in cmd and "--listen-port" in cmd, cmd)

    # ---------------------------------------------------------------- 8 端到端社会
    print("· 端到端：两个真 app + 一条总线")
    real = Society(log=lambda *_: None)
    started = real.start()
    check("社会层上线（总线在**本进程**内）", bool(started.get("port")), started)
    for app in (alpha, beta):
        app.write_source(list(society_program))
        app.write_capabilities(society_caps)
        # 借出清单与订阅都写在 app 自己的配置里：**调用权在被调方**、收件人看订阅
        app.config_path.write_text(
            'storage = "file"\n\n[service]\nlend = ["shout"]\n\n'
            '[collab]\nsubscribe = ["ping"]\n', encoding="utf-8")
    up_rows = real.up([alpha.root, beta.root])
    check("两个实例都握手成功", all(row["ready"] for row in up_rows), up_rows)
    rows = real.rows()
    check("账本里有两条且在跑",
          len([r for r in rows if r.get("alive")]) == 2, rows)
    reply = real.tell("ping", "hello", sender="society")
    check("消息真送达订阅者（TCP 回执）",
          sorted(reply.get("delivered") or []) == ["alpha", "beta"], reply)
    verbs = SocietyOps(real, log=lambda *_: None)
    who = verbs.who("alpha")
    check("who 能拿到对端 hello（真 TCP）", who.get("ok") and who.get("hello"), who)
    fired = verbs.fire("alpha", "#go", "click")
    check("fire：投到**对端自己声明的** handler（真 TCP）", fired["ok"] is True, fired)
    missed = verbs.fire("alpha", "#nope", "click")
    check("fire：没声明的交互**可见拒绝**并列出它声明的（不是静默 no-op）",
          missed["ok"] is False and "#go.click" in (missed.get("note") or ""), missed)
    called = verbs.call("alpha", "shout", {"text": "嗨"})
    check("call：借出能力跨进程同步返回",
          called["ok"] is True and called.get("value") == "嗨", called)
    refused = verbs.call("alpha", "nope", {})
    check("call：没借出的能力被拒（调用权在被调方）", refused["ok"] is False, refused)
    tail = real.bus_tail(5)
    check("总线审计有记录", bool(tail), tail)
    check("总线账本是机器级一条", real.ledger_path().is_file(), real.ledger_path())
    # 窗口实例：复用**同一个 session** 的绑定 ⇒ 它也能收得到消息（此前只发不收）
    print("· 窗口实例也能被投递（`--listen-port` 那只耳朵）")
    from puppethub.hub import _free_port as _free
    from puppethub.remote import TcpBinding, bind_tcp, serve_tcp
    from puppethub.session import Session as _Session
    import threading as _threading

    gamma_dir = AppDir(other)                      # gamma：第 6 节建的那个
    gamma_config = gamma_dir.config_path
    gamma_config.write_text(gamma_config.read_text(encoding="utf-8")
                            + '\n[collab]\nsubscribe = ["ping"]\n', encoding="utf-8")
    win_session = _Session(gamma_dir)               # 窗口实例手里就是**这样一个 session**
    win_session.start()
    win_session.attach_hub(int(real.bus_port()))
    win_port = int(_free(9500, set()))
    win_binding = TcpBinding(session=win_session)   # 复用 session（不是自造一个）
    win_server = bind_tcp(win_port)
    _threading.Thread(target=serve_tcp, args=(win_binding, win_port, win_server),
                      daemon=True, name="smoke-window-ear").start()
    data = real.ledger()
    # pid 刻意给 None：**绝不能填本进程的 pid**——`down` 会真的 SIGTERM 它（测试把自己杀了）
    data["windows"]["gamma"] = {"root": str(Path(other).resolve()),
                                "port": win_port, "pid": None}
    real._save(data)
    check("账本 windows 段记了窗口实例，且被探到在跑",
          any(r["mode"] == "window" and r["alive"] for r in real.rows()),
          real.rows())
    got = real.tell("ping", "给窗口的", sender="society")
    check("窗口实例收得到投递（回执含 gamma）",
          "gamma" in (got.get("delivered") or []), got)
    who_window = SocietyOps(real, log=lambda *_: None).who("gamma")
    check("窗口实例答得了 who", who_window.get("ok") and who_window.get("hello"),
          who_window)

    # 社会层 → 调度官的接线：总线消息 = 刺激源（首页就是把这两条线接起来的）
    wake = orchestrator.Orchestrator(
        SocietyOps(real, log=lambda *_: None), log=lambda *_: None,
        provider=FakeProvider(["（消息已收到，暂不动手。）"]),
        raw_config={"autonomous": {"enable": True, "debounce_seconds": 0.1,
                                   "reflect_every": 0}})
    real.on_peer_message = wake.notify
    wake.start_autonomous()
    baseline = len(wake.audit())
    real.tell("ping", "有人在吗", sender="society")
    deadline = _time.time() + 5.0
    while _time.time() < deadline and len(wake.audit()) <= baseline:
        _time.sleep(0.05)
    wake.stop()
    real.on_peer_message = None
    woke = wake.audit()[baseline:]
    check("总线消息能唤醒调度官（社会层 → 调度官的接线）",
          len(woke) == 1 and "有人在吗" in woke[0].get("request", ""), woke)

    stopped = real.down()
    check("停止实例（无头两个 + 窗口条目如实清账）",
          len(stopped["stopped"]) >= 2 and len(stopped["dead"]) >= 1, stopped)
    real.stop()

    # ---------------------------------------------------------------- 收尾
    print("")
    if failures:
        print("失败 %d 项：%s" % (len(failures), "；".join(failures)))
        return 1
    print("首页冒烟全绿。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
