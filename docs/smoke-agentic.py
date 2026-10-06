"""agent 主体性冒烟：身份 / 交棒 / 事后评价 / 经验固化。

依据本轮四项实现（`docs/review-agent-gap.md` §5 的四个方向）。验的是**边界与语义**：

1. **身份在上下文不在 SYSTEM**：SYSTEM 不再写死"共作者"；共作者轮与自主轮拿到**不同**身份块
2. **交棒有锚**：交棒前打命名快照；写者切到 autonomous；无 LLM 时**明确拒绝**（不打空锚）
3. **事后评价靠机制采证**：`_record_outcome` 区分"改了/没改/被拒/卡住"；
   `retro_block` 把证据注入下一步；不让 LLM 自报
4. **经验固化**：app 级技能可写、路径守卫严（穿越/嵌套被拒）、写完即重载

用法：python docs/smoke-agentic.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "Puppet"))

from puppethub.appdir import create_app          # noqa: E402
from puppethub.session import Session            # noqa: E402
from puppethub.chat import Block, Chat         # noqa: E402
from puppethub.builtin_plugins.default_prompt import DefaultPrompt   # noqa: E402
from puppethub.autonomous import AutonomousRunner  # noqa: E402

FAILED = []


def check(name, ok, detail=""):
    print("  %s %s%s" % ("✓" if ok else "✗", name,
                         ("—— " + str(detail)[:200]) if (detail and not ok) else ""))
    if not ok:
        FAILED.append(name)


PROGRAM = [
    'add #root window #win title="agent 冒烟" w=400 h=300',
    'add #win col #main pad=12 gap=8',
    'add #main button #go text="走"',
    'on #go click:',
    '    set #main title="走了"',
]


def _mk(role, origin):
    """造一份最小 context，只为验身份块。"""
    return {"role": role, "mode": "执行", "source": "add #a text #b", "request": "r",
            "turns": [], "catalog": [], "diagnostics": [], "skills": [],
            "spec": [], "assets": [], "capability_docs": {},
            "vocabulary": {"controls": [], "attributes": [], "animations": [], "icons": []},
            "observation": {}, "vision": {}, "origin": origin}


class _Res:
    """最小 TurnResult 替身（只带 _record_outcome 读得到的字段）。"""
    def __init__(self, applied=(), skipped=(), diags=(), error="", stuck="", halted=False):
        self.applied = list(applied)
        self.skipped = list(skipped)
        self.diagnostics = list(diags)
        self.error = error
        self.stuck = stuck
        self.halted = halted


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="puppethub-smoke-agentic-"))
    app = create_app(work, "agentic", "agent 冒烟")
    app.write_source(PROGRAM, origin="system")
    session = Session(app)
    session.start()

    # ---------------------------------------------------------------- 1
    print("1) 身份在上下文，不在 SYSTEM（共享大脑的前提）")
    p = DefaultPrompt()
    sys_co = p.rewrite(_mk("coauthor", "llm"))["system"]
    sys_op = p.rewrite(_mk("operator", "autonomous"))["system"]
    body_co = p.rewrite(_mk("coauthor", "llm"))["messages"][0]["content"]
    body_op = p.rewrite(_mk("operator", "autonomous"))["messages"][0]["content"]
    check("SYSTEM 不再硬编码「你是共作者」", "**共作者**" not in sys_co, sys_co[:120])
    check("两种身份共用同一套 SYSTEM（共享大脑）", sys_co == sys_op)
    check("共作者轮：身份块说人在场", "共作者" in body_co and "人在场" in body_co,
          body_co[:120])
    check("自主轮：身份块说当值者·没有人在场",
          "当值者" in body_op and "没有人在场" in body_op, body_op[:120])
    check("身份块是上下文第一块（先读到）", body_co.startswith("【身份】"), body_co[:40])
    check("两轮身份不同（不是同一句话）", body_co.split("【模式】")[0] != body_op.split("【模式】")[0])

    # ---------------------------------------------------------------- 2
    print("\n2) 交棒：先打锚，再交棒；无自主回路时明确拒绝")
    class _FakeChat:
        halted = False
        def resume(self, origin="user"):
            pass
    class _FakeRunner:
        def __init__(self):
            self.chat = _FakeChat()
    session.autonomous = _FakeRunner()          # 假装自主回路可用
    session.set_writer("llm", origin="user")
    before_named = len([s for s in session.app.list_snapshots() if s.kind == "named"])
    out = session.handoff_to_autonomous(origin="user")
    check("交棒成功", out.get("ok") is True, out)
    check("写者已切到 autonomous", session.writer == "autonomous", session.writer)
    named = [s for s in session.app.list_snapshots() if s.kind == "named"]
    check("交棒前打了命名快照（回退锚）", len(named) == before_named + 1,
          "%d -> %d" % (before_named, len(named)))
    check("快照标明了是交棒锚",
          any("交棒" in (s.reason or "") for s in named), [s.reason for s in named])
    again = session.handoff_to_autonomous(origin="user")
    check("重复交棒是幂等的（不覆盖锚点）",
          again.get("ok") and again.get("already") is True, again)
    # 无自主回路 → 明确拒绝，且**不切写者**
    session.autonomous = None
    session.set_writer("llm", origin="user") if session.chat is not None else None
    denied = session.handoff_to_autonomous(origin="user")
    check("没有自主回路时交棒被拒", denied.get("ok") is False, denied)
    if session.chat is not None:
        check("被拒后写者仍是 llm（没被切走）", session.writer == "llm", session.writer)

    # ---------------------------------------------------------------- 3
    print("\n3) 事后评价：证据由机制采证，不让 LLM 自报")
    runner = AutonomousRunner.__new__(AutonomousRunner)   # 不跑 __init__，只测纯逻辑
    from collections import deque
    import threading
    runner._outcomes = deque(maxlen=12)
    runner._lock = threading.Lock()
    runner._record_outcome({"time": "T1", "trigger": "点了按钮"},
                           _Res(applied=["命令批 1 行"]))
    runner._record_outcome({"time": "T2", "trigger": "库存低了"},
                           _Res(applied=[], skipped=[]))
    runner._record_outcome({"time": "T3", "trigger": "删数据"},
                           _Res(applied=[], skipped=["自主模式拒绝：能力 x 未列入白名单"]))
    runner._record_outcome({"time": "T4", "trigger": "改程序"},
                           _Res(applied=["命令批 2 行"],
                                diags=[{"code": "BIND_EVAL", "level": "error",
                                        "message": "绑不上"}]))
    block = runner.retro_block()
    check("注入块声明证据来源是机制采证", "机制采证" in block, block[:120])
    check("「改了且无报错」与「改了但报错」分得开",
          "改了，且无报错" in block and "报了错" in block, block)
    check("「决定不改」被认作合法结果（不是失败）",
          "决定不改" in block and "合法结果" in block, block)
    check("「被拒」单独成类（不等于没动）", "被拒" in block, block)
    check("报错带出了诊断码", "BIND_EVAL" in block, block)
    check("空历史时返回空串（不注废话）",
          AutonomousRunner.retro_block(runner) != "" and
          runner.retro_block(limit=4) == block)

    # ---------------------------------------------------------------- 4
    print("\n4) 经验固化：app 级技能可写，路径守卫严")
    g = Chat._skill_path
    check("允许 .puppethub/skills/x.md", g(".puppethub/skills/x.md") is True)
    check("拒绝目录本身", g(".puppethub/skills/") is False)
    check("拒绝路径穿越", g(".puppethub/skills/../../evil.md") is False)
    check("拒绝子目录嵌套（skills.py 只扫一层）",
          g(".puppethub/skills/sub/x.md") is False)
    check("拒绝隐藏文件", g(".puppethub/skills/.x.md") is False)
    check("不放行其它路径", g("assets/x.png") is False and g("capabilities.py") is False)

    # 真写一份技能，验"写完即重载"
    session.autonomous = _FakeRunner()
    skill_body = ("---\nname: 空态文案\nwhen: 列表 空态 没有数据\n"
                  "about: 列表为空要显式说明\n---\n\n空列表必须给文案与下一步。\n")
    chat = Chat(session, provider=None, prompts=[], storage=session.storage,
                memory=None, log=lambda *a: None, origin="llm")
    res = _Res()
    chat._apply_write(Block("write", skill_body, path=".puppethub/skills/空态文案.md"), res)
    written = session.app.root / ".puppethub/skills/空态文案.md"
    check("技能文件真的落盘了", written.is_file(), str(written))
    check("applied 里有「已重载技能」（写完即生效）",
          any("重载技能" in a for a in res.applied), res.applied)
    names = [s.name for s in (session.skills or [])]
    check("新技能进了技能表", "空态文案" in names, names)
    check("写入进了决策流水（可查是谁教的）",
          "app 级技能" in _decisions(session) and "空态文案" in _decisions(session),
          _decisions(session)[-200:])

    print("\n%s" % ("失败：" + "、".join(FAILED) if FAILED else "全部通过"))
    return 1 if FAILED else 0


def _decisions(session):
    """决策流水落在 DESIGN.md（append-only 的表格行），没有读回 API——直接看文件。"""
    try:
        return session.app.read_design() or ""
    except Exception:  # noqa: BLE001
        return ""


if __name__ == "__main__":
    sys.exit(main())
