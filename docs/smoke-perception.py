"""感知（L2）冒烟：把"到手的快照用起来"，让 app 自己察觉到盲区与状态变化。

依据 `docs/discuss-sensing.md` §5 与 §8。验的是**边界**，不是"能跑"：

1. **盲区命中**：点了/改了，但程序里既无处理器也无订阅 → 唤醒自主回路
   （那是 app 里"什么也不会发生"的地方，正是 agent 该补一手的）；
2. **有规则接就不唤醒**：IR 会响应它，唤醒 LLM 是白烧预算；
3. **状态变化感知**：`data` / `slots` / `flags` 相对上一帧变了 → 感知到；
4. **零噪音**：没变就不感知（基线每帧更新，切换写者不会炸出"假变化"）；
5. **非自主当值时不感知**：人在驱动时 agent 不插嘴；
6. **感知只读**：全过程真源逐字节未变。

用法：python docs/smoke-perception.py
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

FAILED = []


def check(name, ok, detail=""):
    print("  %s %s%s" % ("✓" if ok else "✗", name,
                         ("—— " + str(detail)[:200]) if (detail and not ok) else ""))
    if not ok:
        FAILED.append(name)


class _FakeChat:
    """`set_writer` 会看 `autonomous.chat.halted`——给它一个不熔断的替身。"""
    halted = False


class _Recorder:
    """假的自主回路：只记录感知调用，不做决策。"""

    def __init__(self):
        self.chat = _FakeChat()
        self.interactions = []
        self.state_changes = []

    def on_interaction(self, target, event):
        self.interactions.append((target, event))

    def on_state_change(self, changes):
        self.state_changes.append(list(changes))

    # session 的其他感知入口——本冒烟不关心，给 no-op 免得 AttributeError
    def on_diagnostic(self, *a, **k):
        pass

    def on_peer_message(self, *a, **k):
        pass


PROGRAM = [
    'add #root window #win title="感知冒烟" w=400 h=300',
    'add #win col #main pad=12 gap=8',
    'add #main text #t text="hi"',
    'add #main button #go text="走"',
    'add #main button #orphan text="孤儿"',
    'on #go click:',
    '    set #t text="走了"',
    'data #items = [1, 2] of {n: int}',
    'add #main text #count text=str(count(#items))',
]


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="puppethub-smoke-perception-"))
    app = create_app(work, "perception", "感知冒烟")
    app.write_source(PROGRAM, origin="system")

    session = Session(app)
    session.start()
    rec = _Recorder()
    session.autonomous = rec
    session.set_writer("autonomous", origin="user")
    source_before = session.program_text()

    print("1) 盲区判据：无处可去才唤醒")
    session.fire("orphan", "click")
    check("点了没有处理器的 #orphan → 感知到盲区",
          ("orphan", "click") in rec.interactions, rec.interactions)
    check("盲区描述带上了控件与事件",
          any(t == "orphan" and e == "click" for t, e in rec.interactions),
          rec.interactions)
    seen = len(rec.interactions)

    print("\n2) 有规则接 → 不唤醒（不白烧预算）")
    session.fire("go", "click")
    check("点了有 handler 的 #go → 没有新增感知",
          len(rec.interactions) == seen, rec.interactions)
    check("#go 的 handler 确实在程序里（前提成立）",
          any(h.target == "go" and h.event == "click"
              for h in session.engine.program.handlers),
          [(h.target, h.event) for h in session.engine.program.handlers])

    print("\n3) 状态变化感知：快照已经在手，只是过去没用")
    session.refresh()                      # 第一帧只建基线
    check("第一帧只建基线，不算变化", rec.state_changes == [], rec.state_changes)
    session.engine.items["items"] = [{"n": 3}]     # 直接改引擎状态：这里验的是 diff
    session.refresh()
    check("数据源变了 → 感知到", len(rec.state_changes) == 1, rec.state_changes)
    if rec.state_changes:
        kinds = {(c[0], c[1]) for c in rec.state_changes[0]}
        check("变化被描述成 (类别, 地址)",
              ("data", "#items") in kinds, sorted(kinds))

    print("\n4) 零噪音：没变就不感知")
    session.refresh()
    check("再 refresh（无变化）→ 没有新增感知",
          len(rec.state_changes) == 1, rec.state_changes)

    print("\n5) 非自主当值时不感知（人在驱动时 agent 不插嘴）")
    session.set_writer("llm", origin="user")
    before_i, before_s = len(rec.interactions), len(rec.state_changes)
    session.fire("orphan", "click")
    session.engine.items["items"] = [{"n": 9}]
    session.refresh()
    check("共作者当值：盲区不唤醒", len(rec.interactions) == before_i, rec.interactions)
    check("共作者当值：状态变化不唤醒", len(rec.state_changes) == before_s, rec.state_changes)

    print("\n6) 感知只读：感知本身不碰真源")
    # 注意：第 2 节点了有 handler 的 #go，真源**本来就该变**（handler 生效了）。
    # 所以这里要单独测"只有感知发生"的那一段——孤儿点击无 handler，真源不该动。
    session.set_writer("autonomous", origin="user")
    before_readonly = session.program_text()
    session.fire("orphan", "click")
    session.refresh()
    check("感知全程真源逐字节未变", session.program_text() == before_readonly,
          "感知写了真源——这是只读性被破坏")
    check("感知确实发生过（否则上面这条是空断言）",
          len(rec.interactions) > before_i, rec.interactions)

    print("\n%s" % ("失败：" + "、".join(FAILED) if FAILED else "全部通过"))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
